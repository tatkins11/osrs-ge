package com.mapletree.geexport;

import net.runelite.client.config.Config;
import net.runelite.client.config.ConfigGroup;
import net.runelite.client.config.ConfigItem;

@ConfigGroup("geexport")
public interface GeExportConfig extends Config
{
	@ConfigItem(
		keyName = "apiUrl",
		name = "API URL",
		description = "Base URL of your GE Terminal (no trailing slash)",
		position = 0
	)
	default String apiUrl()
	{
		return "https://ge.mapletree-ge.com";
	}

	@ConfigItem(
		keyName = "apiKey",
		name = "API key",
		description = "The OSRS_GE_INGEST_TOKEN configured on your server",
		position = 1
	)
	default String apiKey()
	{
		return "";
	}

	@ConfigItem(
		keyName = "enabled",
		name = "Send orders",
		description = "Stream your live GE offers to the server (read-only; never places offers)",
		position = 2
	)
	default boolean enabled()
	{
		return true;
	}
}
