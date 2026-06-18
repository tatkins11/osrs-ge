package com.mapletree.geexport;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.reflect.TypeToken;
import com.google.inject.Provides;
import java.io.IOException;
import java.lang.reflect.Type;
import java.time.Instant;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import javax.inject.Inject;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GrandExchangeOffer;
import net.runelite.api.GrandExchangeOfferState;
import net.runelite.api.Player;
import net.runelite.api.events.GrandExchangeOfferChanged;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * Streams your live Grand Exchange offers (buy/sell, fill progress, cancels) to your
 * GE Terminal. READ-ONLY: it only observes and reports — it never places, edits, or
 * cancels offers (that would be botting). Completed fills auto-log as trades server-side.
 */
@Slf4j
@PluginDescriptor(
	name = "GE Terminal Export",
	description = "Streams your live GE offers to your GE Terminal (read-only)",
	tags = {"ge", "grand", "exchange", "flipping", "export"}
)
public class GeExportPlugin extends Plugin
{
	private static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");
	private static final String GROUP = "geexport";
	private static final Type MAP_TYPE = new TypeToken<HashMap<Integer, String>>() {}.getType();

	@Inject private Client client;
	@Inject private GeExportConfig config;
	@Inject private OkHttpClient http;
	@Inject private Gson gson;
	@Inject private ConfigManager configManager;

	// slot -> stable order id + the offer signature it was minted for. Persisted across
	// sessions so a still-open (or completed-but-uncollected) offer keeps ONE id on relogin,
	// which is what stops the server creating a duplicate order/trade.
	private final Map<Integer, String> slotId = new HashMap<>();
	private final Map<Integer, String> slotSig = new HashMap<>();

	@Override
	protected void startUp()
	{
		restore();
		log.info("GE Terminal Export started");
	}

	@Override
	protected void shutDown()
	{
		slotId.clear();
		slotSig.clear();
	}

	@Provides
	GeExportConfig provideConfig(ConfigManager cm)
	{
		return cm.getConfig(GeExportConfig.class);
	}

	@Subscribe
	public void onGrandExchangeOfferChanged(GrandExchangeOfferChanged e)
	{
		if (!config.enabled() || config.apiKey().trim().isEmpty())
		{
			return;
		}
		final int slot = e.getSlot();
		final GrandExchangeOffer offer = e.getOffer();
		if (offer == null)
		{
			return;
		}
		final GrandExchangeOfferState state = offer.getState();

		if (state == GrandExchangeOfferState.EMPTY)
		{
			// slot cleared / collected — drop its mapping; nothing to record
			if (slotId.remove(slot) != null)
			{
				slotSig.remove(slot);
				persist();
			}
			return;
		}

		final boolean sell = state == GrandExchangeOfferState.SELLING
			|| state == GrandExchangeOfferState.SOLD
			|| state == GrandExchangeOfferState.CANCELLED_SELL;
		final String side = sell ? "sell" : "buy";
		final String sig = offer.getItemId() + ":" + offer.getPrice() + ":" + offer.getTotalQuantity() + ":" + side;

		// a slot's offer signature is fixed for the life of the offer; a different sig = a new offer = a new id
		String id = slotId.get(slot);
		if (id == null || !sig.equals(slotSig.get(slot)))
		{
			id = UUID.randomUUID().toString();
			slotId.put(slot, id);
			slotSig.put(slot, sig);
			persist();
		}

		final Player me = client.getLocalPlayer();
		final JsonObject o = new JsonObject();
		o.addProperty("order_id", id);
		o.addProperty("login", me != null ? me.getName() : null);
		o.addProperty("slot", slot);
		o.addProperty("item_id", offer.getItemId());
		o.addProperty("side", side);
		o.addProperty("price", offer.getPrice());
		o.addProperty("total_qty", offer.getTotalQuantity());
		o.addProperty("filled_qty", offer.getQuantitySold());
		o.addProperty("spent", offer.getSpent());
		o.addProperty("state", state.name());
		o.addProperty("ts", Instant.now().toString());

		final JsonArray offers = new JsonArray();
		offers.add(o);
		final JsonObject body = new JsonObject();
		body.add("offers", offers);
		post(body);
	}

	private void post(JsonObject body)
	{
		final String base = config.apiUrl().trim().replaceAll("/+$", "");
		final Request req = new Request.Builder()
			.url(base + "/api/ge-offers")
			.header("Authorization", "Bearer " + config.apiKey().trim())
			.post(RequestBody.create(JSON, gson.toJson(body)))
			.build();
		http.newCall(req).enqueue(new Callback()
		{
			@Override
			public void onFailure(Call call, IOException ex)
			{
				log.debug("GE export POST failed", ex);
			}

			@Override
			public void onResponse(Call call, Response response)
			{
				try (Response r = response)
				{
					if (!r.isSuccessful())
					{
						log.debug("GE export HTTP {}", r.code());
					}
				}
			}
		});
	}

	private void persist()
	{
		configManager.setConfiguration(GROUP, "slotId", gson.toJson(slotId));
		configManager.setConfiguration(GROUP, "slotSig", gson.toJson(slotSig));
	}

	private void restore()
	{
		try
		{
			final String a = configManager.getConfiguration(GROUP, "slotId");
			final String b = configManager.getConfiguration(GROUP, "slotSig");
			if (a != null)
			{
				final Map<Integer, String> m = gson.fromJson(a, MAP_TYPE);
				if (m != null)
				{
					slotId.putAll(m);
				}
			}
			if (b != null)
			{
				final Map<Integer, String> m = gson.fromJson(b, MAP_TYPE);
				if (m != null)
				{
					slotSig.putAll(m);
				}
			}
		}
		catch (Exception ex)
		{
			log.debug("could not restore slot map", ex);
		}
	}
}
