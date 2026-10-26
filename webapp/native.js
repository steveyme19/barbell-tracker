// Extras for the Android app (Capacitor, docs/android.md). In a normal browser
// isNativeApp() is false and nothing else here gets called.
//
// No bundler, so no @capacitor/core import: the bridge puts window.Capacitor in the
// app, with a ready-made proxy for every native plugin in Capacitor.Plugins.

export function isNativeApp() {
  return !!window.Capacitor?.isNativePlatform?.();
}

const plugins = () => window.Capacitor.Plugins;

/** Keep recording with the screen off (foreground service + wake lock), or stop. ask: false
 * leaves the notification prompt for later (askNotifications), e.g. before the pod's Nearby
 * devices prompt: Android cancels a second prompt that comes while one is up. */
export async function recordInBackground(on, { ask = true } = {}) {
  await plugins().BarSensors.record({ on, ask });
}

/** The "Recording session" notification's permission (Android 13+), asked once. */
export async function askNotifications() {
  await plugins().BarSensors.askNotifications();
}

/** Vibrate [on, off, on, ...] ms, natively (works with the screen off). */
export async function vibrateNative(pattern) {
  await plugins().BarSensors.vibrate({ pattern: Array.isArray(pattern) ? pattern : [pattern] });
}

/** Keep the screen on (FLAG_KEEP_SCREEN_ON on the app window), or let it sleep again. */
export async function keepScreenOn(on) {
  await plugins().BarSensors.keepScreenOn({ on });
}

/**
 * Write a text file to Documents/BarTracker/[folder/]name on the phone, no permission
 * needed (BarSensorsPlugin.saveText). Returns { uri, path }.
 */
export async function saveFile(folder, name, text) {
  return plugins().BarSensors.saveText({ folder, name, text });
}

/** Delete a file saveFile wrote (e.g. a set re-saved under a different lift name). */
export async function deleteFile(folder, name) {
  return plugins().BarSensors.deleteText({ folder, name });
}

/** Open the share sheet for saved files (Drive, a chat, email). Closing it isn't an error. */
export async function shareFiles(uris, title) {
  try {
    await plugins().Share.share({ title, files: uris });
  } catch (err) {
    if (!/cancel/i.test(err.message)) throw err;
  }
}
