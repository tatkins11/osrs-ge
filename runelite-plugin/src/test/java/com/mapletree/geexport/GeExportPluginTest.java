package com.mapletree.geexport;

import net.runelite.client.RuneLite;
import net.runelite.client.externalplugins.ExternalPluginManager;

/**
 * Dev launcher: starts RuneLite with this plugin loaded (developer mode).
 *   ./gradlew run
 */
public class GeExportPluginTest
{
	public static void main(String[] args) throws Exception
	{
		ExternalPluginManager.loadBuiltin(GeExportPlugin.class);
		RuneLite.main(args);
	}
}
