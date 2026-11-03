package fr.lezardia.app;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.graphics.Insets;
import android.graphics.Matrix;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Size;
import android.view.Surface;
import android.view.TextureView;
import android.view.WindowInsets;
import android.widget.TextView;

import java.nio.ByteBuffer;
import java.util.Arrays;

/**
 * Lit le QR code « Connecter un téléphone » du PC avec la caméra arrière (Camera2) et le décode (QrDecoder, ZXing).
 * Renvoie le texte lu (EXTRA_TEXT) ; c'est MainActivity qui décide si c'est bien une adresse de Lezard IA.
 * Seule la luminance (plan Y) des images est lue, en mémoire, rien n'est enregistré.
 */
public class ScanActivity extends Activity {
    static final String EXTRA_TEXT = "text";
    private static final int REQ_CAMERA = 1;
    private static final int MAX_PIXELS = 1920 * 1080;

    private final QrDecoder decoder = new QrDecoder();
    private TextureView preview;
    private TextView hint;
    private HandlerThread thread;
    private Handler background;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private ImageReader frames;
    private Size size;
    private volatile boolean done;
    private boolean asked;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_scan);
        preview = findViewById(R.id.preview);
        hint = findViewById(R.id.scan_hint);
        findViewById(R.id.scan_cancel).setOnClickListener(v -> finish());
        preview.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
            @Override
            public void onSurfaceTextureAvailable(SurfaceTexture t, int w, int h) {
                open();
            }

            @Override
            public void onSurfaceTextureSizeChanged(SurfaceTexture t, int w, int h) {
                fit();
            }

            @Override
            public boolean onSurfaceTextureDestroyed(SurfaceTexture t) {
                return true;
            }

            @Override
            public void onSurfaceTextureUpdated(SurfaceTexture t) {
            }
        });
        if (Build.VERSION.SDK_INT >= 35) {                   // Android 15 : sous les barres du système
            findViewById(R.id.scan_root).setOnApplyWindowInsetsListener((v, insets) -> {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return WindowInsets.CONSUMED;
            });
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        thread = new HandlerThread("scan");
        thread.start();
        background = new Handler(thread.getLooper());
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            if (preview.isAvailable()) open();                // sinon : à l'arrivée de la surface
        } else if (!asked) {                                  // une seule demande : un refus ne boucle pas
            asked = true;
            requestPermissions(new String[] {Manifest.permission.CAMERA}, REQ_CAMERA);
        }
    }

    @Override
    protected void onPause() {
        closeCamera();
        thread.quitSafely();
        super.onPause();
    }

    @Override
    public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        if (request != REQ_CAMERA) return;
        // accordée : la caméra s'ouvre au retour au premier plan (onResume), qui suit la fenêtre de permission
        if (results.length == 0 || results[0] != PackageManager.PERMISSION_GRANTED) {
            hint.setText("Caméra refusée : autorisez-la dans les réglages de l'application, ou saisissez l'adresse "
                    + "du PC à la main.");
        }
    }

    private void open() {
        if (camera != null || checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return;
        CameraManager manager = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
        try {
            String id = null;
            for (String candidate : manager.getCameraIdList()) {
                Integer facing = manager.getCameraCharacteristics(candidate).get(CameraCharacteristics.LENS_FACING);
                if (id == null || (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK)) id = candidate;
            }
            if (id == null) {
                hint.setText("Aucune caméra sur cet appareil : saisissez l'adresse du PC à la main.");
                return;
            }
            StreamConfigurationMap map = manager.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            size = pickSize(map.getOutputSizes(ImageFormat.YUV_420_888));
            frames = ImageReader.newInstance(size.getWidth(), size.getHeight(), ImageFormat.YUV_420_888, 2);
            frames.setOnImageAvailableListener(this::decode, background);
            fit();
            manager.openCamera(id, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice device) {
                    camera = device;
                    startPreview();
                }

                @Override
                public void onDisconnected(CameraDevice device) {
                    device.close();
                    camera = null;
                }

                @Override
                public void onError(CameraDevice device, int error) {
                    device.close();
                    camera = null;
                    runOnUiThread(() -> hint.setText("La caméra ne répond pas (erreur " + error + ")."));
                }
            }, background);
        } catch (CameraAccessException | SecurityException | IllegalArgumentException e) {
            hint.setText("Caméra indisponible : saisissez l'adresse du PC à la main.");
        }
    }

    /** La plus grande taille d'image jusqu'à 1080p : assez fine pour un QR à 30 cm, assez légère à décoder. */
    private static Size pickSize(Size[] sizes) {
        Size best = sizes[0];
        for (Size s : sizes) {
            int px = s.getWidth() * s.getHeight();
            int bestPx = best.getWidth() * best.getHeight();
            if (px <= MAX_PIXELS && (bestPx > MAX_PIXELS || px > bestPx)) best = s;
        }
        return best;
    }

    /** Aperçu recadré au centre (sans déformation) : l'image paysage du capteur s'affiche tournée en portrait. */
    private void fit() {
        if (size == null || preview.getWidth() == 0) return;
        float w = preview.getWidth(), h = preview.getHeight();
        float content = (float) size.getHeight() / size.getWidth();     // largeur / hauteur une fois en portrait
        Matrix m = new Matrix();
        if (w / h < content) m.setScale(content * h / w, 1f, w / 2, h / 2);
        else m.setScale(1f, w / content / h, w / 2, h / 2);
        preview.setTransform(m);
    }

    private void startPreview() {
        try {
            SurfaceTexture texture = preview.getSurfaceTexture();
            if (texture == null || camera == null) return;
            texture.setDefaultBufferSize(size.getWidth(), size.getHeight());
            Surface surface = new Surface(texture);
            CaptureRequest.Builder request = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            request.addTarget(surface);
            request.addTarget(frames.getSurface());
            request.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
            camera.createCaptureSession(Arrays.asList(surface, frames.getSurface()),
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(CameraCaptureSession s) {
                            if (camera == null) return;
                            session = s;
                            try {
                                s.setRepeatingRequest(request.build(), null, background);
                            } catch (CameraAccessException | IllegalStateException e) {
                                runOnUiThread(() -> hint.setText("La caméra ne répond pas."));
                            }
                        }

                        @Override
                        public void onConfigureFailed(CameraCaptureSession s) {
                            runOnUiThread(() -> hint.setText("La caméra ne répond pas."));
                        }
                    }, background);
        } catch (CameraAccessException | IllegalStateException e) {
            runOnUiThread(() -> hint.setText("La caméra ne répond pas."));
        }
    }

    private void decode(ImageReader source) {
        Image image = source.acquireLatestImage();
        if (image == null) return;
        int width = image.getWidth(), height = image.getHeight();
        byte[] luminance = new byte[width * height];
        try {
            Image.Plane y = image.getPlanes()[0];
            ByteBuffer buffer = y.getBuffer();
            int stride = y.getRowStride();
            for (int row = 0; row < height; row++) {
                buffer.position(row * stride);
                buffer.get(luminance, row * width, width);
            }
        } finally {
            image.close();
        }
        if (done) return;
        // image du capteur non tournée : le QR est lu quelle que soit son orientation
        String text = decoder.decode(luminance, width, height);
        if (text == null) return;                         // pas de QR lisible : on attend l'image suivante
        done = true;
        runOnUiThread(() -> {
            setResult(RESULT_OK, new Intent().putExtra(EXTRA_TEXT, text));
            finish();
        });
    }

    private void closeCamera() {
        if (session != null) session.close();
        if (camera != null) camera.close();
        if (frames != null) frames.close();
        session = null;
        camera = null;
        frames = null;
    }
}
