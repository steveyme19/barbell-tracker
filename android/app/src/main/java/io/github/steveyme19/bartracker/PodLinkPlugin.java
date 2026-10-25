package io.github.steveyme19.bartracker;

import android.Manifest;
import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.ParcelUuid;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.ArrayDeque;
import java.util.Collections;
import java.util.Locale;
import java.util.UUID;

/**
 * The pod over Bluetooth LE for the app (docs/ble-plan.md section 3 "Android app", step 11;
 * protocol docs/ble.md). A dumb pipe: it connects, subscribes to DATA (STATUS frames ride on it
 * too, D28) and hands every frame to JS untouched. It never parses one: the only decoders are
 * podlink.py and the PodStream port in webapp/sources.js, so no third one can drift.
 *
 * Methods:
 *   connect({ any? }) -> { name, address, mtu, link, wallMs, elapsedMs }: the saved pod with a 10 s
 *     timeout, else an 8 s scan for the PodLink service. The scan takes the saved pod the moment it
 *     hears it; with none saved, the strongest. It never swaps the saved pod for another one unless
 *     any: true (pick a new pod: skip the saved one, take the strongest); the address is saved once
 *     a connect works (it comes from FICR, so it stays the same across reboots). Then the connection order
 *     (docs/ble.md 7): services, MTU 247, read INFO, read STATUS (both go out as frames), the DATA
 *     CCCD. wallMs / elapsedMs map the events' arrival times to the wall clock.
 *   write({ b, link? }): b = base64 bytes, written to CTRL with response (queued). With link, a
 *     write meant for an earlier link (JS computed it before a reconnect) is refused.
 *   reread(): read INFO and STATUS again, both out as frames (the decoder's need-info).
 *   disconnect(): the last batch out, then disconnect and close.
 * Events:
 *   "pod" { f: [base64 frame, ...], w: [arrival, SystemClock.elapsedRealtime() ms, ...] }, ~50 ms batches
 *   "podState" { state: connecting | ready | lost | closed, status, mtu, link }
 * A link that drops without disconnect() is reconnected with autoConnect (it waits for the pod by
 * itself, screen off too); the connection order runs again and "ready" (with a new link number)
 * says JS can write its STREAM command (CCCDs don't persist without bonding). Bluetooth switched
 * off counts as a drop, and the reconnect waits until it's back on. A link that goes 5 s without a
 * frame is dead whatever Android says (the pod sends a STATUS every second).
 * Android 13+ only: the pod's address is random static, and only getRemoteLeDevice (API 33) can say
 * so. On 12 a reconnect after a Bluetooth restart waits forever for a public address.
 */
@SuppressLint("MissingPermission")        // every Bluetooth call runs after the alias is granted
@CapacitorPlugin(name = "PodLink", permissions = @Permission(alias = "bluetooth",
        strings = {Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT}))
public class PodLinkPlugin extends Plugin {

    private static final String TAG = "PodLink";
    static final UUID SERVICE = UUID.fromString("9ba40001-6de5-429c-b225-8134b9953e9d");
    static final UUID DATA = UUID.fromString("9ba40002-6de5-429c-b225-8134b9953e9d");
    static final UUID CTRL = UUID.fromString("9ba40003-6de5-429c-b225-8134b9953e9d");
    static final UUID STATUS = UUID.fromString("9ba40004-6de5-429c-b225-8134b9953e9d");
    static final UUID INFO = UUID.fromString("9ba40005-6de5-429c-b225-8134b9953e9d");
    static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    private static final long BATCH_MS = 50;               // frames to JS every ~50 ms
    private static final long CONNECT_TIMEOUT_MS = 10_000;  // a direct connect to the saved pod
    private static final long SCAN_MS = 8_000;
    private static final long SETUP_TIMEOUT_MS = 30_000;    // connect() as a whole (connect, scan, connect)
    private static final long AUTO_RETRY_MS = 2_000;        // between autoConnect tries that fail at once
    private static final long SILENT_MS = 5_000;            // no frame this long on a ready link: it's dead
    private static final int MAX_133 = 3;                   // GATT_ERROR (133) before a link came up: retry
    private static final int GATT_ERROR = 133;
    private static final int SILENT = 8;                    // our status for a dead link (like a supervision timeout)

    // the connection order, one GATT operation at a time; a callback only moves it on from its own step
    private enum Step { IDLE, CONNECTING, DISCOVER, MTU, INFO, STATUS, CCCD, READY }

    private HandlerThread thread;      // every GATT call and all the state below run here, in order
    private Handler h;

    private BluetoothDevice device;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic chData, chCtrl, chInfo, chStatus;
    private Step step = Step.IDLE;
    private BluetoothLeScanner scanner;
    private ScanCallback scanCb;
    private BluetoothDevice best;      // the strongest pod the scan heard
    private int bestRssi;
    private PluginCall permCall;       // connect() waiting on the Nearby devices prompt
    private PluginCall pending;        // connect() until the connection order is done
    private boolean any;               // that connect() may pick (and save) a pod other than the saved one
    private final ArrayDeque<Op> ops = new ArrayDeque<>();   // write() / reread() on a ready link; the head runs
    private boolean opRunning;
    private boolean wanted;            // JS wants the link (false after disconnect())
    private boolean autoConnecting;    // reconnecting with autoConnect after a drop
    private int mtu = 23;
    private boolean mtuAsked;          // our requestMtu is out (an unrequested onMtuChanged must not move the chain)
    private int retries;               // 133 retries on this connect
    private int link;                  // +1 every time the connection order completes ("ready")
    private int session;               // +1 at every close / failed setup: a delayed retry from an older one gives up
    private long lastFrame;            // elapsedRealtime of the latest frame, for the watchdog
    private JSArray bf, bw;            // the batch being filled: frames, arrival times
    private boolean flushQueued;
    private BroadcastReceiver adapterRx;

    private final Runnable flushTask = this::flush;
    private final Runnable connectTimeout = this::onConnectTimeout;
    private final Runnable scanEnd = this::onScanEnd;
    private final Runnable autoRetry = this::autoConnect;
    private final Runnable watchdog = this::checkSilence;
    private final Runnable setupTimeout = () -> failSetup("Couldn't reach the pod. Bump it to wake it, keep it close, and try again.");

    private static final class Op {
        final PluginCall call;
        final byte[] bytes;            // a CTRL write, or null: reread INFO and STATUS
        Op(PluginCall call, byte[] bytes) { this.call = call; this.bytes = bytes; }
    }

    @Override
    public void load() {
        thread = new HandlerThread("PodLink");
        thread.start();
        h = new Handler(thread.getLooper());
        h.post(this::newBatch);
        // Bluetooth switched off: Android can drop our GATT client without a word (no
        // onConnectionStateChange), so watch the adapter ourselves. Delivered on our thread.
        adapterRx = new BroadcastReceiver() {
            @Override
            public void onReceive(Context ctx, Intent i) {
                onAdapterState(i.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR));
            }
        };
        IntentFilter f = new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getContext().registerReceiver(adapterRx, f, null, h, Context.RECEIVER_NOT_EXPORTED);
        } else {
            getContext().registerReceiver(adapterRx, f, null, h);
        }
    }

    // ---------------------------------------------------------------------------------------
    // methods

    @PluginMethod
    public void connect(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            call.reject("The pod needs Android 13 or newer.");
            return;
        }
        h.post(() -> {
            if (pending != null || permCall != null) {
                call.reject("Already connecting to the pod.");
                return;
            }
            if (getPermissionState("bluetooth") == PermissionState.GRANTED) {
                startConnect(call);
                return;
            }
            permCall = call;                                // disconnect() can cancel it while the prompt is up
            requestPermissionForAlias("bluetooth", call, "permissionDone");   // the Nearby devices prompt
        });
    }

    @PermissionCallback
    private void permissionDone(PluginCall call) {
        h.post(() -> {
            // cancelled by disconnect() meanwhile: Capacitor then hands the answer in with a null call
            if (call == null || call != permCall) return;
            permCall = null;
            if (getPermissionState("bluetooth") == PermissionState.GRANTED) startConnect(call);
            else call.reject("Bluetooth (Nearby devices) wasn't allowed. Allow it in the app's settings to use the pod.");
        });
    }

    @PluginMethod
    public void write(PluginCall call) {
        String b = call.getString("b");
        if (b == null) {
            call.reject("write needs { b: base64 }.");
            return;
        }
        byte[] bytes;
        try {
            bytes = Base64.decode(b, Base64.DEFAULT);
        } catch (IllegalArgumentException e) {
            call.reject("write: b isn't base64.");
            return;
        }
        Integer forLink = call.getInt("link");
        h.post(() -> {
            if (step != Step.READY) {
                call.reject("The pod isn't connected.");
                return;
            }
            if (forLink != null && forLink != link) {       // computed before a reconnect: maybe another boot
                call.reject("Stale link: the pod reconnected since.");
                return;
            }
            ops.add(new Op(call, bytes));
            nextOp();
        });
    }

    @PluginMethod
    public void reread(PluginCall call) {
        h.post(() -> {
            if (step != Step.READY) {
                call.reject("The pod isn't connected.");
                return;
            }
            ops.add(new Op(call, null));
            nextOp();
        });
    }

    @PluginMethod
    public void disconnect(PluginCall call) {
        h.post(() -> {
            close("closed");
            call.resolve();
        });
    }

    @Override
    protected void handleOnDestroy() {
        if (h == null) return;
        try { getContext().unregisterReceiver(adapterRx); } catch (IllegalArgumentException ignored) {}
        h.post(() -> close("closed"));
        thread.quitSafely();
        try { thread.join(500); } catch (InterruptedException ignored) {}   // let that last batch go out
    }

    // ---------------------------------------------------------------------------------------
    // connecting

    private void startConnect(PluginCall call) {
        BluetoothAdapter ad = adapter();
        if (ad == null) {
            call.reject("This phone has no Bluetooth.");
            return;
        }
        if (!ad.isEnabled()) {
            call.reject("Turn Bluetooth on.");
            return;
        }
        close(null);                                        // a link from an earlier session
        wanted = true;
        pending = call;
        any = Boolean.TRUE.equals(call.getBoolean("any", false));
        retries = 0;
        autoConnecting = false;
        h.postDelayed(setupTimeout, SETUP_TIMEOUT_MS);
        state("connecting", 0);
        String addr = any ? null : saved();             // any: pick a new pod, the strongest in range
        if (addr != null && BluetoothAdapter.checkBluetoothAddress(addr)) {
            // the pod's address is random static (BLE_GAP_ADDR_TYPE_RANDOM_STATIC)
            device = ad.getRemoteLeDevice(addr, BluetoothDevice.ADDRESS_TYPE_RANDOM);
            openGatt(false);
            h.postDelayed(connectTimeout, CONNECT_TIMEOUT_MS);
        } else {
            scan();
        }
    }

    private void openGatt(boolean auto) {
        Log.i(TAG, (auto ? "autoConnect " : "connect ") + device.getAddress());
        step = Step.CONNECTING;
        gatt = device.connectGatt(getContext(), auto, cb, BluetoothDevice.TRANSPORT_LE);
        if (gatt != null) return;
        step = Step.IDLE;
        if (auto) h.postDelayed(autoRetry, AUTO_RETRY_MS);  // the adapter went away: try again
        else failOrDrop("Couldn't start a Bluetooth connection.", GATT_ERROR);
    }

    /** Reconnect after a drop: autoConnect waits for the pod by itself, screen off too. Only with
     * Bluetooth fully on (in between, Android hands out a client that never calls back); the
     * adapter receiver calls this again when it is. */
    private void autoConnect() {
        h.removeCallbacks(autoRetry);
        if (!wanted || gatt != null || pending != null || device == null) return;
        BluetoothAdapter ad = adapter();
        if (ad == null || ad.getState() != BluetoothAdapter.STATE_ON) return;
        autoConnecting = true;
        openGatt(true);
    }

    /** The saved pod didn't answer in 10 s (asleep, away): look for it, or for one. */
    private void onConnectTimeout() {
        if (pending == null || gatt == null || step.ordinal() > Step.CONNECTING.ordinal()) return;
        Log.i(TAG, "direct connect timed out, scanning");
        closeGatt();
        scan();
    }

    private void scan() {
        BluetoothAdapter ad = adapter();
        scanner = ad != null ? ad.getBluetoothLeScanner() : null;
        if (scanner == null) {
            failSetup("Turn Bluetooth on.");
            return;
        }
        String mine = any ? null : saved();
        best = null;
        bestRssi = Integer.MIN_VALUE;
        final ScanCallback me = new ScanCallback() {
            @Override
            public void onScanResult(int type, ScanResult r) {
                h.post(() -> {
                    if (scanCb != this || pending == null) return;   // a stopped scan's late result
                    BluetoothDevice d = r.getDevice();      // a scanned device knows its address type
                    if (d.getAddress().equals(mine)) {      // the saved pod woke up: take it now
                        best = d;
                        bestRssi = Integer.MAX_VALUE;
                        onScanEnd();
                    } else if (r.getRssi() > bestRssi && bestRssi != Integer.MAX_VALUE) {
                        bestRssi = r.getRssi();
                        best = d;
                    }
                });
            }

            @Override
            public void onScanFailed(int code) {
                h.post(() -> { if (scanCb == this) failSetup("Bluetooth scan failed (" + code + ")."); });
            }
        };
        scanCb = me;
        ScanFilter f = new ScanFilter.Builder().setServiceUuid(new ParcelUuid(SERVICE)).build();
        ScanSettings s = new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        scanner.startScan(Collections.singletonList(f), s, me);
        Log.i(TAG, "scanning for the pod");
        h.postDelayed(scanEnd, SCAN_MS);
    }

    private void onScanEnd() {
        stopScan();
        if (pending == null || gatt != null) return;
        String mine = saved();
        if (best == null) {
            failSetup("No pod found. Bump it to wake it and keep it close.");
            return;
        }
        if (mine != null && !best.getAddress().equals(mine) && !any) {
            // never swap the bar's pod for another one in range without being asked to
            failSetup("Your pod didn't answer (another pod is nearby). Bump yours to wake it and try again.");
            return;
        }
        Log.i(TAG, "found " + best.getAddress() + (bestRssi == Integer.MAX_VALUE ? " (the saved pod)" : " at " + bestRssi + " dBm"));
        device = best;
        retries = 0;
        autoConnecting = false;
        openGatt(false);                                    // the setup timeout covers this one
    }

    private void stopScan() {
        h.removeCallbacks(scanEnd);
        if (scanner != null && scanCb != null) {
            try { scanner.stopScan(scanCb); } catch (Exception ignored) {}   // Bluetooth went off meanwhile
        }
        scanCb = null;
    }

    private void onAdapterState(int s) {
        if (s == BluetoothAdapter.STATE_TURNING_OFF || s == BluetoothAdapter.STATE_OFF) {
            if (pending != null) {
                failSetup("Bluetooth went off.");
                return;
            }
            if (gatt == null && step == Step.IDLE) return;
            Log.i(TAG, "Bluetooth going off");
            boolean was = step == Step.READY;
            flush();
            failOps("Bluetooth went off.");
            h.removeCallbacks(autoRetry);
            closeGatt();
            if (was) state("lost", SILENT);
            autoConnecting = true;                          // the next try is a reconnect
        } else if (s == BluetoothAdapter.STATE_ON && wanted && pending == null) {
            Log.i(TAG, "Bluetooth back on");
            autoConnect();
        }
    }

    // ---------------------------------------------------------------------------------------
    // the GATT chain: one operation at a time, each started from the previous one's callback

    private final BluetoothGattCallback cb = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            h.post(() -> onLinkState(g, status, newState));
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            h.post(() -> onServices(g, status));
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int m, int status) {
            h.post(() -> onMtu(g, m, status));
        }

        // Android 13+ hands the value in (the only versions we run on). Don't call super: it calls
        // the old overload, and the frame would go to JS twice.
        @Override
        public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value, int status) {
            arrived(g, c, value, status, true);
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
            arrived(g, c, value, BluetoothGatt.GATT_SUCCESS, false);
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
            h.post(() -> onCccd(g, status));
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            h.post(() -> onWrite(g, status));
        }
    };

    /** A read or a notification, on Bluetooth's thread: stamp it now, copy it (the value array is
     * Bluetooth's), then hand it to ours. */
    private void arrived(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value, int status, boolean read) {
        long w = SystemClock.elapsedRealtime();
        byte[] copy = value != null ? value.clone() : null;
        UUID u = c.getUuid();
        h.post(() -> {
            if (g != gatt) return;                          // a closed link's late callback
            if (status == BluetoothGatt.GATT_SUCCESS && copy != null) add(copy, w);
            if (read) onRead(u, status);
        });
    }

    private void onLinkState(BluetoothGatt g, int status, int newState) {
        if (g != gatt) {                                    // an old link: we already closed it
            g.close();
            return;
        }
        if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
            if (step != Step.CONNECTING) return;
            h.removeCallbacks(connectTimeout);
            Log.i(TAG, "connected " + g.getDevice().getAddress());
            step = Step.DISCOVER;
            if (!g.discoverServices()) dropped(GATT_ERROR);
            return;
        }
        dropped(status);
    }

    /** The link went (or never came up). A link that never came up retries a 133 (Android's
     * common connect failure); then connect() pending fails. A ready link: "lost", then autoConnect
     * until the pod is back. Not wanted: just tidy up. */
    private void dropped(int status) {
        boolean was = step == Step.READY;
        failOps("The pod's link dropped.");
        closeGatt();
        if (!wanted) return;
        if (status == GATT_ERROR && retries < MAX_133 && !was) {
            retries++;
            Log.i(TAG, "status 133, retry " + retries);
            boolean auto = autoConnecting;
            int s0 = session;
            h.postDelayed(() -> {
                if (session != s0 || !wanted || gatt != null) return;   // a later connect() owns the link now
                if (auto) {
                    autoConnect();
                } else if (pending != null) {
                    openGatt(false);
                    h.postDelayed(connectTimeout, CONNECT_TIMEOUT_MS);   // still falls back to a scan
                }
            }, 500);
            return;
        }
        if (pending != null) {
            failSetup("The pod's link dropped while connecting (status " + status + "). Try again.");
            return;
        }
        Log.i(TAG, "link lost, status " + status);
        if (was) state("lost", status);
        retries = 0;
        // the first try at once; one that failed without ever connecting waits a little
        if (was || !autoConnecting) autoConnect();
        else h.postDelayed(autoRetry, AUTO_RETRY_MS);
    }

    private void onServices(BluetoothGatt g, int status) {
        if (g != gatt || step != Step.DISCOVER) return;
        BluetoothGattService svc = status == BluetoothGatt.GATT_SUCCESS ? g.getService(SERVICE) : null;
        if (svc == null) {
            failOrDrop("That isn't a pod (no PodLink service).", status);
            return;
        }
        chData = svc.getCharacteristic(DATA);
        chCtrl = svc.getCharacteristic(CTRL);
        chInfo = svc.getCharacteristic(INFO);
        chStatus = svc.getCharacteristic(STATUS);
        if (chData == null || chCtrl == null || chInfo == null || chStatus == null) {
            failOrDrop("The pod's service is missing a characteristic.", status);
            return;
        }
        step = Step.MTU;
        mtuAsked = true;
        if (!g.requestMtu(247)) onMtu(g, 23, BluetoothGatt.GATT_FAILURE);   // carry on at 23
    }

    private void onMtu(BluetoothGatt g, int m, int status) {
        if (g != gatt) return;
        if (status == BluetoothGatt.GATT_SUCCESS) mtu = m;   // an unrequested one is a true value too
        if (step != Step.MTU || !mtuAsked) {                // not our reply: never start a GATT op from it
            Log.i(TAG, "mtu " + mtu + " (unrequested)");
            return;
        }
        mtuAsked = false;
        Log.i(TAG, "mtu " + mtu + (status == BluetoothGatt.GATT_SUCCESS ? "" : " (request failed: " + status + ")"));
        step = Step.INFO;
        if (!g.readCharacteristic(chInfo)) failOrDrop("Couldn't read the pod's INFO.", GATT_ERROR);
    }

    private void onRead(UUID u, int status) {
        if (step == Step.READY) {                           // a reread() op
            onOpRead(u, status);
            return;
        }
        if (status != BluetoothGatt.GATT_SUCCESS) {
            failOrDrop("Couldn't read the pod (status " + status + ").", status);
            return;
        }
        if (step == Step.INFO && u.equals(INFO)) {
            step = Step.STATUS;
            if (!gatt.readCharacteristic(chStatus)) failOrDrop("Couldn't read the pod's STATUS.", GATT_ERROR);
        } else if (step == Step.STATUS && u.equals(STATUS)) {
            subscribe();
        }
    }

    /** Notifications on DATA: the only CCCD (STATUS frames ride on DATA, D28). */
    private void subscribe() {
        step = Step.CCCD;
        BluetoothGattDescriptor d = chData.getDescriptor(CCCD);
        if (d == null || !gatt.setCharacteristicNotification(chData, true)
                || gatt.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) != BluetoothStatusCodes.SUCCESS) {
            failOrDrop("Couldn't subscribe to the pod's DATA.", GATT_ERROR);
        }
    }

    private void onCccd(BluetoothGatt g, int status) {
        if (g != gatt || step != Step.CCCD) return;
        Log.i(TAG, "DATA CCCD write, status " + status);
        if (status != BluetoothGatt.GATT_SUCCESS) {
            failOrDrop("Couldn't subscribe to the pod's DATA (status " + status + ").", status);
            return;
        }
        step = Step.READY;
        retries = 0;
        link++;
        lastFrame = SystemClock.elapsedRealtime();
        h.postDelayed(watchdog, 1_000);
        flush();                                            // INFO and STATUS reach JS before the resolve
        BluetoothDevice dev = g.getDevice();
        String name = dev.getName();
        if (name == null) name = prefs().getString("name:" + dev.getAddress(), null);
        if (name == null) name = nameFor(dev.getAddress());   // a direct connect before any scan: no name yet
        prefs().edit().putString("name:" + dev.getAddress(), name).apply();
        if (pending != null) {
            h.removeCallbacks(setupTimeout);
            // the pod that answered becomes the saved one (a picked new pod, or the first)
            if (saved() == null || any || dev.getAddress().equals(saved())) {
                prefs().edit().putString("address", dev.getAddress()).apply();
            }
            JSObject res = new JSObject();
            res.put("name", name);
            res.put("address", dev.getAddress());
            res.put("mtu", mtu);
            res.put("link", link);
            res.put("wallMs", System.currentTimeMillis());
            res.put("elapsedMs", SystemClock.elapsedRealtime());
            PluginCall c = pending;
            pending = null;
            c.resolve(res);
        }
        state("ready", 0);
    }

    /** "BarPod-E26C" for F5:EA:43:DB:E2:6C: the pod names itself after its address's last two bytes. */
    private static String nameFor(String addr) {
        String hex = addr.replace(":", "");
        return "BarPod-" + hex.substring(hex.length() - 4).toUpperCase(Locale.ROOT);
    }

    /** A ready link that sends nothing for 5 s is dead, whatever Android says (Bluetooth off
     * can drop our client without a callback). The pod sends a STATUS every second. */
    private void checkSilence() {
        if (step != Step.READY) return;
        if (SystemClock.elapsedRealtime() - lastFrame > SILENT_MS) {
            Log.w(TAG, "no frame for " + SILENT_MS + " ms: the link is dead");
            dropped(SILENT);
            return;
        }
        h.postDelayed(watchdog, 1_000);
    }

    /** A step of the connection order failed: connect() pending gets the reason, a running link
     * counts it as a drop (and reconnects). */
    private void failOrDrop(String why, int status) {
        Log.w(TAG, why);
        if (pending != null) failSetup(why);
        else dropped(status);
    }

    private void failSetup(String why) {
        flush();                                            // a failed setup's INFO / STATUS reach JS first
        session++;
        h.removeCallbacks(setupTimeout);
        h.removeCallbacks(connectTimeout);
        h.removeCallbacks(autoRetry);
        stopScan();
        closeGatt();
        wanted = false;
        if (pending != null) {
            PluginCall c = pending;
            pending = null;
            c.reject(why);
        }
        state("closed", 0);
    }

    // ---------------------------------------------------------------------------------------
    // ops on a ready link, one at a time: CTRL writes and rereads

    private void nextOp() {
        if (opRunning || ops.isEmpty() || step != Step.READY) return;
        Op op = ops.peek();
        boolean ok = op.bytes == null
                ? gatt.readCharacteristic(chInfo)          // reread: INFO, then STATUS (onOpRead)
                : gatt.writeCharacteristic(chCtrl, op.bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS;
        if (!ok) {
            ops.poll();
            op.call.reject(op.bytes == null ? "Couldn't read the pod." : "Couldn't write to the pod.");
            nextOp();
            return;
        }
        opRunning = true;
    }

    private void onWrite(BluetoothGatt g, int status) {
        if (g != gatt || !opRunning || ops.isEmpty() || ops.peek().bytes == null) return;
        opRunning = false;
        Op op = ops.poll();
        if (status == BluetoothGatt.GATT_SUCCESS) op.call.resolve();
        else op.call.reject("The pod refused the write (status " + status + ").");
        nextOp();
    }

    private void onOpRead(UUID u, int status) {
        if (!opRunning || ops.isEmpty() || ops.peek().bytes != null) return;
        if (status != BluetoothGatt.GATT_SUCCESS) {
            opRunning = false;
            ops.poll().call.reject("Couldn't read the pod (status " + status + ").");
            nextOp();
            return;
        }
        if (u.equals(INFO)) {
            if (!gatt.readCharacteristic(chStatus)) {
                opRunning = false;
                ops.poll().call.reject("Couldn't read the pod's STATUS.");
                nextOp();
            }
            return;
        }
        opRunning = false;
        flush();                                            // the frames reach JS before the resolve
        ops.poll().call.resolve();
        nextOp();
    }

    private void failOps(String why) {
        opRunning = false;
        while (!ops.isEmpty()) ops.poll().call.reject(why);
    }

    // ---------------------------------------------------------------------------------------
    // frames to JS, and tidying up

    private void add(byte[] frame, long w) {
        lastFrame = w;
        bf.put(Base64.encodeToString(frame, Base64.NO_WRAP));
        bw.put(w);
        if (!flushQueued) {
            flushQueued = true;
            h.postDelayed(flushTask, BATCH_MS);
        }
    }

    private void flush() {
        h.removeCallbacks(flushTask);
        flushQueued = false;
        if (bf == null || bf.length() == 0) return;
        JSObject batch = new JSObject();
        batch.put("f", bf);
        batch.put("w", bw);
        notifyListeners("pod", batch);
        newBatch();
    }

    private void newBatch() {
        bf = new JSArray();
        bw = new JSArray();
    }

    /** disconnect() and onDestroy: the last batch out, then the link down. why = the podState to
     * report (null: none, a new connect() follows). */
    private void close(String why) {
        boolean had = gatt != null || pending != null || permCall != null || wanted;
        wanted = false;
        session++;
        flush();
        h.removeCallbacks(setupTimeout);
        h.removeCallbacks(connectTimeout);
        h.removeCallbacks(autoRetry);
        stopScan();
        if (permCall != null) {
            PluginCall c = permCall;
            permCall = null;
            c.reject("Disconnected.");
        }
        if (pending != null) {
            PluginCall c = pending;
            pending = null;
            c.reject("Disconnected.");
        }
        failOps("Disconnected.");
        if (gatt != null) gatt.disconnect();
        closeGatt();
        if (why != null && had) state(why, 0);
    }

    private void closeGatt() {
        h.removeCallbacks(watchdog);
        h.removeCallbacks(connectTimeout);
        step = Step.IDLE;
        mtuAsked = false;
        if (gatt != null) gatt.close();
        gatt = null;
        chData = chCtrl = chInfo = chStatus = null;
    }

    private void state(String s, int status) {
        flush();                                            // no state passes a frame that came before it
        JSObject o = new JSObject();
        o.put("state", s);
        o.put("status", status);
        o.put("mtu", mtu);
        o.put("link", link);
        notifyListeners("podState", o);
    }

    private BluetoothAdapter adapter() {
        BluetoothManager bm = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
        return bm != null ? bm.getAdapter() : null;
    }

    private SharedPreferences prefs() {
        return getContext().getSharedPreferences("PodLink", Context.MODE_PRIVATE);
    }

    private String saved() {
        return prefs().getString("address", null);
    }
}
