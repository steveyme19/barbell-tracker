package io.github.steveyme19.bartracker;

import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.net.Uri;
import android.os.Environment;
import android.os.HandlerThread;
import android.view.WindowManager;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import androidx.core.content.ContextCompat;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * Phone accelerometer + gyro at the fastest rate the phone allows (docs/android.md A2),
 * plus keepScreenOn, saveText and deleteText (A3, session mode), and record / vibrate for
 * screen-off sessions (RecordingService).
 *
 * Emits "samples" events, each a batch of about 50 ms:
 *   { t: [...], ax: [...], ay: [...], az: [...], gx: [...], gy: [...], gz: [...] }
 * t is seconds since start() on the sensor's own clock (SensorEvent.timestamp), never
 * the time the batch reaches JS. Acceleration in m/s^2 including gravity, gyro in deg/s,
 * same device axes as the browser's DeviceMotion, so it's the D4 sample format.
 */
@CapacitorPlugin(name = "BarSensors")
public class BarSensorsPlugin extends Plugin implements SensorEventListener {

    private static final long BATCH_NS = 50_000_000L;   // flush to JS every ~50 ms
    private static final double RAD_TO_DEG = 180.0 / Math.PI;

    private SensorManager sm;
    private Sensor accel;
    private Sensor gyro;
    private HandlerThread thread;    // sensor callbacks run here, off the UI thread

    // Everything below is only touched on the sensor thread.
    private long t0 = -1;            // timestamp of the first accel event, ns
    private long lastFlush = 0;
    private float gx, gy, gz;        // latest gyro reading, deg/s
    private boolean haveGyro;        // false until the first gyro event
    private JSArray bt, bax, bay, baz, bgx, bgy, bgz;

    @Override
    public void load() {
        sm = (SensorManager) getContext().getSystemService(Context.SENSOR_SERVICE);
        accel = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
        gyro = sm.getDefaultSensor(Sensor.TYPE_GYROSCOPE);
    }

    @PluginMethod
    public void start(PluginCall call) {
        if (accel == null) {
            call.reject("This phone has no accelerometer.");
            return;
        }
        stopSensors();
        thread = new HandlerThread("BarSensors");
        thread.start();
        Handler h = new Handler(thread.getLooper());
        h.post(() -> {   // reset state on the sensor thread so no callback sees it half done
            t0 = -1;
            gx = gy = gz = 0;
            haveGyro = (gyro == null);   // no gyro: never wait for one
            newBatch();
        });
        // Delay 0 = SENSOR_DELAY_FASTEST. Above 200 Hz needs HIGH_SAMPLING_RATE_SENSORS (manifest).
        sm.registerListener(this, accel, SensorManager.SENSOR_DELAY_FASTEST, h);
        if (gyro != null) sm.registerListener(this, gyro, SensorManager.SENSOR_DELAY_FASTEST, h);

        JSObject info = new JSObject();
        info.put("accel", accel.getName());
        info.put("accelMaxHz", accel.getMinDelay() > 0 ? 1e6 / accel.getMinDelay() : 0);
        info.put("gyro", gyro != null ? gyro.getName() : null);
        info.put("gyroMaxHz", gyro != null && gyro.getMinDelay() > 0 ? 1e6 / gyro.getMinDelay() : 0);
        call.resolve(info);
    }

    @PluginMethod
    public void stop(PluginCall call) {
        stopSensors();
        call.resolve();
    }

    /** Keep the screen on during a set (A3). { on: true } sets the window flag, false clears it. */
    /**
     * { on: true } starts RecordingService (session keeps running with the screen off), false stops it.
     * ask: false skips the notification prompt (a pod session asks for Nearby devices next, and
     * Android cancels a second permission prompt that comes while one is up: askNotifications() later).
     */
    @PluginMethod
    public void record(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("on", true));
        Context ctx = getContext();
        Intent svc = new Intent(ctx, RecordingService.class);
        if (on && Boolean.TRUE.equals(call.getBoolean("ask", true))) askForNotifications();
        try {
            if (on) ContextCompat.startForegroundService(ctx, svc);
            else ctx.stopService(svc);
            call.resolve();
        } catch (Exception e) {
            call.reject("Couldn't " + (on ? "start" : "stop") + " background recording: " + e.getMessage());
        }
    }

    /** The "Recording session" notification's permission (Android 13+; asked once, recording works either way). */
    @PluginMethod
    public void askNotifications(PluginCall call) {
        askForNotifications();
        call.resolve();
    }

    private void askForNotifications() {
        Context ctx = getContext();
        if (Build.VERSION.SDK_INT >= 33 && getActivity() != null
                && ContextCompat.checkSelfPermission(ctx, "android.permission.POST_NOTIFICATIONS") != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            getActivity().requestPermissions(new String[] {"android.permission.POST_NOTIFICATIONS"}, 7);
        }
    }

    /**
     * Vibrate a pattern { pattern: [on, off, on, ...] } in ms. Native, because the WebView's
     * navigator.vibrate is unreliable while the page is hidden (screen off).
     */
    @PluginMethod
    public void vibrate(PluginCall call) {
        Vibrator v = (Vibrator) getContext().getSystemService(Context.VIBRATOR_SERVICE);
        JSArray arr = call.getArray("pattern", new JSArray());
        try {
            long[] timings = new long[arr.length() + 1];               // starts with a 0 ms delay
            for (int i = 0; i < arr.length(); i++) timings[i + 1] = arr.getLong(i);
            if (v != null && v.hasVibrator() && arr.length() > 0) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) v.vibrate(VibrationEffect.createWaveform(timings, -1));
                else v.vibrate(timings, -1);
            }
            call.resolve();
        } catch (Exception e) {
            call.reject("Couldn't vibrate: " + e.getMessage());
        }
    }

    @PluginMethod
    public void keepScreenOn(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("on", true));
        // Window flags have to be changed on the UI thread.
        getActivity().runOnUiThread(() -> {
            if (on) getActivity().getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            else getActivity().getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            call.resolve();
        });
    }

    /**
     * Save a text file to Documents/BarTracker/[folder/]name on the phone (A3, session
     * mode). { name, text, folder? } -> { uri, path }. Android 11+ lets an app create
     * files in the shared Documents folder without a storage permission. (Our own method
     * instead of @capacitor/filesystem, which needs a Java 21 toolchain, see D16.)
     */
    @PluginMethod
    public void saveText(PluginCall call) {
        File file = target(call);
        if (file == null) return;
        File dir = file.getParentFile();
        if (!dir.isDirectory() && !dir.mkdirs()) {
            call.reject("Couldn't create " + dir);
            return;
        }
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(call.getString("text", "").getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            call.reject("Couldn't write " + file + ": " + e.getMessage());
            return;
        }
        JSObject res = new JSObject();
        res.put("uri", Uri.fromFile(file).toString());   // the share plugin turns this into a content:// URI
        res.put("path", file.getAbsolutePath());
        call.resolve(res);
    }

    /** Delete a file saveText wrote (a set re-saved under a new lift name). { name, folder? } */
    @PluginMethod
    public void deleteText(PluginCall call) {
        File file = target(call);
        if (file == null) return;
        JSObject res = new JSObject();
        res.put("deleted", file.exists() && file.delete());
        call.resolve(res);
    }

    /** Documents/BarTracker/[folder/]name from the call, or null (and rejected) if unsafe. */
    private File target(PluginCall call) {
        String name = call.getString("name");
        String folder = call.getString("folder", "");
        if (!safe(name) || !(folder.isEmpty() || safe(folder))) {
            call.reject("Bad file or folder name.");
            return null;
        }
        File dir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS), "BarTracker");
        if (!folder.isEmpty()) dir = new File(dir, folder);
        return new File(dir, name);
    }

    // One plain path piece: no slashes, no "..".
    private static boolean safe(String s) {
        return s != null && !s.isEmpty() && !s.contains("/") && !s.contains("\\") && !s.equals("..") && !s.equals(".");
    }

    @Override
    protected void handleOnDestroy() {
        stopSensors();
    }

    private void stopSensors() {
        sm.unregisterListener(this);
        if (thread != null) {
            // Flush what's left, then end the thread after that runs.
            Handler h = new Handler(thread.getLooper());
            h.post(this::flush);
            thread.quitSafely();
            // Wait for that last flush, so JS gets every sample before stop() resolves.
            try { thread.join(500); } catch (InterruptedException ignored) {}
            thread = null;
        }
    }

    @Override
    public void onSensorChanged(SensorEvent e) {
        if (e.sensor.getType() == Sensor.TYPE_GYROSCOPE) {
            gx = (float) (e.values[0] * RAD_TO_DEG);
            gy = (float) (e.values[1] * RAD_TO_DEG);
            gz = (float) (e.values[2] * RAD_TO_DEG);
            haveGyro = true;
            return;
        }
        // One sample per accel event, paired with the most recent gyro reading. Skip the
        // first few ms before the gyro has reported, so no sample carries a fake 0 deg/s.
        if (!haveGyro) return;
        if (t0 < 0) {
            t0 = e.timestamp;
            lastFlush = e.timestamp;
        }
        bt.put(Double.valueOf((e.timestamp - t0) / 1e9));
        add(bax, e.values[0]);
        add(bay, e.values[1]);
        add(baz, e.values[2]);
        add(bgx, gx);
        add(bgy, gy);
        add(bgz, gz);
        if (e.timestamp - lastFlush >= BATCH_NS) {
            lastFlush = e.timestamp;
            flush();
        }
    }

    @Override
    public void onAccuracyChanged(Sensor sensor, int accuracy) {}

    private void flush() {
        if (bt == null || bt.length() == 0) return;
        JSObject batch = new JSObject();
        batch.put("t", bt);
        batch.put("ax", bax);
        batch.put("ay", bay);
        batch.put("az", baz);
        batch.put("gx", bgx);
        batch.put("gy", bgy);
        batch.put("gz", bgz);
        notifyListeners("samples", batch);
        newBatch();
    }

    // put(Object) instead of put(double), which throws a checked exception. Going through
    // Float.toString keeps a float like 9.81f as 9.81 instead of 9.8100004196167.
    private static void add(JSArray a, float v) {
        a.put(Double.valueOf(Float.toString(v)));
    }

    private void newBatch() {
        bt = new JSArray();
        bax = new JSArray();
        bay = new JSArray();
        baz = new JSArray();
        bgx = new JSArray();
        bgy = new JSArray();
        bgz = new JSArray();
    }
}
