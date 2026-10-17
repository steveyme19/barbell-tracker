package io.github.steveyme19.bartracker;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;

/**
 * Keeps a session recording with the screen off (docs/android.md, "Recording with the
 * screen off"). A foreground service of type "health" (fitness tracking; Android 14+
 * wants a type, and this one needs HIGH_SAMPLING_RATE_SENSORS, which we declare) plus a
 * partial wake lock, so the CPU keeps taking sensor events while the phone is locked.
 * The sensor listener itself stays in BarSensorsPlugin; this only keeps the app awake.
 */
public class RecordingService extends Service {

    private static final String CHANNEL = "recording";
    private static final int NOTIFICATION_ID = 1;
    private static final long MAX_HOLD_MS = 4L * 60 * 60 * 1000;   // a forgotten session can't drain the battery forever

    private PowerManager.WakeLock wakeLock;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Recording", NotificationManager.IMPORTANCE_LOW));
        }
        // Tapping the notification brings the app back.
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        Notification n = new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle("Bar Tracker")
                .setContentText("Recording session")
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
        int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
                ? ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH : 0;
        ServiceCompat.startForeground(this, NOTIFICATION_ID, n, type);

        if (wakeLock == null) {
            PowerManager pm = getSystemService(PowerManager.class);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "BarTracker:recording");
            wakeLock.acquire(MAX_HOLD_MS);
        }
        return START_NOT_STICKY;     // if Android kills it, the app's Resume bar takes over
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
