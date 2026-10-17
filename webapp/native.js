// Extras for the Android app (Capacitor, docs/android.md). In a normal browser
// isNativeApp() is false and nothing else here gets called.
//
// No bundler, so no @capacitor/core import: the bridge puts window.Capacitor in the
// app, with a ready-made proxy for every native plugin in Capacitor.Plugins.

export function isNativeApp() {
  return !!window.Capacitor?.isNativePlatform?.();
}

const plugins = () => window.Capacitor.Plugins;

/** Keep the screen on (FLAG_KEEP_SCREEN_ON on the app window), or let it sleep again. */
export async function keepScreenOn(on) {
  await plugins().BarSensors.keepScreenOn({ on });
}

/**
 * Write a CSV to Documents/BarTracker/ on the phone, then open the share sheet so it
 * can go on to Drive, a chat or email. The file stays saved even if sharing is
 * cancelled. Returns where it was saved.
 */
export async function saveAndShare(name, text) {
  // Our own plugin method (BarSensorsPlugin.saveText); no storage permission needed.
  const { uri } = await plugins().BarSensors.saveText({ name, text });
  try {
    await plugins().Share.share({ title: name, files: [uri] });
  } catch (err) {
    if (!/cancel/i.test(err.message)) throw err;   // closing the share sheet isn't an error
  }
  return `Documents/BarTracker/${name}`;
}
