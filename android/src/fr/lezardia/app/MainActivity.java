package fr.lezardia.app;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Insets;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.UnknownHostException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.net.ssl.SSLException;

/**
 * Lezard IA sur Android : l'interface web du PC (gateway/web) dans une WebView. L'application garde seulement
 * l'adresse du PC ; la clé d'accès reste dans le stockage de la page, comme dans un navigateur (auth.js).
 * Le pont « LezardApp » (enregistrer un fichier, copier, changer de serveur) n'agit que pour les pages du PC.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "lezard";
    private static final String PREF_SERVER = "server";
    private static final int REQ_FILE = 1;
    private static final int REQ_SAVE = 2;
    private static final int REQ_MIC = 3;
    private static final int REQ_CAMERA = 4;
    private static final int REQ_SCAN = 5;
    // clé web « lz.<nom>.<clé> » (31.6, même règle que site/assets/web.js) : ouvre https://<nom>.lezard-ia.fr
    private static final Pattern WEB_KEY =
            Pattern.compile("^lz\\.([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\\.(\\S{16,})$");
    private static final Pattern LOCAL_HOST =
            Pattern.compile("^(localhost|\\d{1,3}(\\.\\d{1,3}){3}|[^/:]+\\.local)(:\\d+)?(/.*)?$", Pattern.CASE_INSENSITIVE);
    // navigator.clipboard absent en HTTP (réseau local) et peu fiable dans une WebView : copie par le pont
    private static final String CLIPBOARD_JS = "(function(){if(!window.LezardApp)return;"
            + "var c=navigator.clipboard;if(!c){c={};try{Object.defineProperty(navigator,'clipboard',"
            + "{value:c,configurable:true});}catch(e){return;}}"
            + "c.writeText=function(t){window.LezardApp.copyText(String(t));return Promise.resolve();};})();";

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private WebView web;
    private View setup;
    private View errorPanel;
    private ProgressBar progress;
    private EditText address;
    private TextView setupError;
    private TextView errorText;
    private Button connect;
    private Button setupBack;
    private String server;                       // « https://hôte[:port] », sans « / » final ; null avant la 1re connexion
    private String failedUrl;
    private ValueCallback<Uri[]> fileCallback;
    private Uri photoUri;
    private PermissionRequest pendingMic;
    private Runnable afterCamera;                // pièce jointe en attente de la permission CAMERA
    private byte[] pendingSave;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_main);
        web = findViewById(R.id.web);
        setup = findViewById(R.id.setup);
        errorPanel = findViewById(R.id.error_panel);
        progress = findViewById(R.id.progress);
        address = findViewById(R.id.address);
        setupError = findViewById(R.id.setup_error);
        errorText = findViewById(R.id.error_text);
        connect = findViewById(R.id.connect);
        setupBack = findViewById(R.id.setup_back);

        // Android 15 dessine sous les barres du système : on décale le contenu, clavier compris
        if (Build.VERSION.SDK_INT >= 35) {
            findViewById(R.id.root).setOnApplyWindowInsetsListener((v, insets) -> {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                Insets ime = insets.getInsets(WindowInsets.Type.ime());
                v.setPadding(bars.left, bars.top, bars.right, Math.max(bars.bottom, ime.bottom));
                return WindowInsets.CONSUMED;
            });
        }

        findViewById(R.id.scan).setOnClickListener(v -> startActivityForResult(new Intent(this, ScanActivity.class), REQ_SCAN));
        connect.setOnClickListener(v -> connect());
        address.setOnEditorActionListener((v, action, event) -> {
            if (action != EditorInfo.IME_ACTION_GO) return false;
            connect();
            return true;
        });
        setupBack.setOnClickListener(v -> setup.setVisibility(View.GONE));
        findViewById(R.id.error_retry).setOnClickListener(v -> {
            errorPanel.setVisibility(View.GONE);
            web.loadUrl(failedUrl != null ? failedUrl : server + "/app");
        });
        findViewById(R.id.error_change).setOnClickListener(v -> showSetup());

        configureWeb();
        server = getSharedPreferences(PREFS, MODE_PRIVATE).getString(PREF_SERVER, null);
        if (server == null) showSetup();
        else if (state == null || web.restoreState(state) == null) web.loadUrl(server + "/app");
    }

    private void configureWeb() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);      // lecture à voix haute lancée après la synthèse
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setSupportMultipleWindows(false);                 // target=_blank : même vue, puis navigateur si externe
        s.setUserAgentString(s.getUserAgentString() + " LezardIA-Android/" + versionName());
        CookieManager.getInstance().setAcceptCookie(true);  // session d'administration (double authentification)
        web.addJavascriptInterface(new Bridge(), "LezardApp");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
    }

    // ---------- écran de connexion ----------

    private void showSetup() {
        errorPanel.setVisibility(View.GONE);
        setupError.setVisibility(View.GONE);
        setupBack.setVisibility(server != null ? View.VISIBLE : View.GONE);
        if (server != null) address.setText(server);
        setup.setVisibility(View.VISIBLE);
        address.requestFocus();
        address.selectAll();
    }

    private void setupMessage(String text) {
        setupError.setText(text);
        setupError.setVisibility(text == null ? View.GONE : View.VISIBLE);
    }

    /** Ce que la personne a saisi : adresse du PC à retenir et page à ouvrir. null si rien de reconnaissable. */
    private static String[] parse(String raw) {
        Matcher m = WEB_KEY.matcher(raw);
        if (m.matches()) return new String[] {"https://" + m.group(1) + ".lezard-ia.fr", "/#key=" + Uri.encode(m.group(2))};
        if (raw.isEmpty() || raw.contains(" ")) return null;
        String text = raw.matches("(?i)^https?://.*") ? raw : (LOCAL_HOST.matcher(raw).matches() ? "http://" : "https://") + raw;
        Uri u = Uri.parse(text);
        if (u.getHost() == null || u.getHost().isEmpty()) return null;
        String fragment = u.getEncodedFragment();             // lien d'accès / QR code (12.6) : <adresse>/#key=<clé>
        return new String[] {origin(u), fragment != null && fragment.startsWith("key=") ? "/#" + fragment : "/app"};
    }

    private void connect() {
        connect(address.getText().toString().trim());
    }

    private void connect(String raw) {
        String[] target = parse(raw);
        if (target == null) {
            setupMessage("Ce n'est pas une adresse de Lezard IA. Scannez le QR code « Connecter un téléphone » du PC, "
                    + "ou saisissez une clé web (lz.…), un lien d'accès ou l'adresse de votre PC.");
            return;
        }
        setupMessage(null);
        connect.setEnabled(false);
        connect.setText("Connexion…");
        io.execute(() -> {
            String[] probe = probe(target[0]);
            runOnUiThread(() -> {
                connect.setEnabled(true);
                connect.setText(R.string.setup_connect);
                if (probe[1] != null) {
                    setupMessage(probe[1]);
                    return;
                }
                saveServer(probe[0]);
                address.setText("");
                ((InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE))
                        .hideSoftInputFromWindow(address.getWindowToken(), 0);
                setup.setVisibility(View.GONE);
                web.clearHistory();
                web.loadUrl(server + target[1]);
            });
        });
    }

    /** Vérifie que l'adresse mène à Lezard IA (/health) : {adresse retenue (après redirection), erreur ou null}. */
    private String[] probe(String origin) {
        for (int hop = 0; hop < 3; hop++) {
            HttpURLConnection c = null;
            try {
                URL url = new URL(origin + "/health");
                c = (HttpURLConnection) url.openConnection();
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
                c.setInstanceFollowRedirects(false);    // http → https (tunnel) : à suivre nous-mêmes
                int code = c.getResponseCode();
                if (code >= 300 && code < 400 && c.getHeaderField("Location") != null) {
                    Uri next = Uri.parse(new URL(url, c.getHeaderField("Location")).toString());
                    if (next.getHost() == null) break;
                    origin = origin(next);
                    continue;
                }
                if (code == 502 || code == 503 || code == 504 || code == 530) {
                    return new String[] {origin, unreachable() + " (erreur " + code + ")"};
                }
                String body = code == 200 ? read(c.getInputStream()) : "";
                if (!body.matches("(?s).*\"status\"\\s*:\\s*\"ok\".*")) {
                    return new String[] {origin, "Cette adresse ne mène pas à Lezard IA (réponse " + code + ")."};
                }
                return new String[] {origin, null};
            } catch (UnknownHostException e) {
                return new String[] {origin, "Adresse introuvable. Vérifiez-la, et que le téléphone a accès à Internet."};
            } catch (SSLException e) {
                return new String[] {origin, "Certificat HTTPS refusé. En réseau local, installez d'abord le certificat "
                        + "de votre PC sur le téléphone, ou utilisez l'adresse http://."};
            } catch (IOException | IllegalArgumentException e) {
                return new String[] {origin, unreachable()};
            } finally {
                if (c != null) c.disconnect();
            }
        }
        return new String[] {origin, "Cette adresse redirige trop de fois."};
    }

    private static String unreachable() {
        return "Votre PC ne répond pas. Est-il allumé, avec Lezard IA lancé ? En Wi-Fi, le téléphone doit être sur le "
                + "même réseau et le mode réseau local activé.";
    }

    private static String read(InputStream in) throws IOException {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            for (int n; out.size() < 4096 && (n = s.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    private void saveServer(String origin) {
        server = origin;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_SERVER, origin).apply();
    }

    // ---------- adresses ----------

    private static String origin(Uri u) {
        String host = u.getHost();
        if (host.contains(":")) host = "[" + host + "]";              // IPv6
        return u.getScheme().toLowerCase() + "://" + host.toLowerCase() + (u.getPort() > 0 ? ":" + u.getPort() : "");
    }

    private static int port(Uri u) {
        return u.getPort() > 0 ? u.getPort() : ("https".equalsIgnoreCase(u.getScheme()) ? 443 : 80);
    }

    /** Page du PC (même hôte ; passage http → https accepté sur les ports par défaut). */
    private boolean isServer(Uri u) {
        if (server == null || u == null || u.getHost() == null) return false;
        String scheme = u.getScheme();
        if (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme)) return false;
        Uri s = Uri.parse(server);
        if (!u.getHost().equalsIgnoreCase(s.getHost())) return false;
        return port(u) == port(s) || (u.getPort() <= 0 && s.getPort() <= 0);
    }

    private boolean onServerPage() {
        String url = web.getUrl();
        return url != null && isServer(Uri.parse(url));
    }

    private void openExternal(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u).addCategory(Intent.CATEGORY_BROWSABLE));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "Aucune application pour ouvrir ce lien.", Toast.LENGTH_SHORT).show();
        }
    }

    private void showError(String url, String message) {
        failedUrl = url;
        errorText.setText(message);
        progress.setVisibility(View.GONE);
        if (setup.getVisibility() != View.VISIBLE) errorPanel.setVisibility(View.VISIBLE);
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (isServer(u)) return false;
            String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase();
            if (scheme.equals("http") || scheme.equals("https") || scheme.equals("mailto") || scheme.equals("tel")) {
                openExternal(u);                                   // site, documentation, compte : navigateur
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            Uri u = Uri.parse(url);
            // passé en https (redirection de la gateway via le tunnel) : on retient la nouvelle adresse
            if (isServer(u) && !origin(u).equals(server) && u.getPort() <= 0) saveServer(origin(u));
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (isServer(Uri.parse(url))) view.evaluateJavascript(CLIPBOARD_JS, null);
            CookieManager.getInstance().flush();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame()) return;
            int code = error.getErrorCode();
            String message = code == ERROR_HOST_LOOKUP ? "Adresse introuvable. Le téléphone a-t-il accès à Internet ?"
                    : (code == ERROR_CONNECT || code == ERROR_TIMEOUT) ? unreachable()
                    : String.valueOf(error.getDescription());
            showError(request.getUrl().toString(), message);
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            int code = response.getStatusCode();
            if (request.isForMainFrame() && (code == 502 || code == 503 || code == 504 || code == 530)) {
                showError(request.getUrl().toString(), unreachable() + " (erreur " + code + ")");
            }
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel();
            showError(error.getUrl(), "Certificat HTTPS refusé. En réseau local, installez d'abord le certificat de "
                    + "votre PC sur le téléphone.");
        }
    }

    // ---------- micro, pièces jointes, progression ----------

    private class Chrome extends WebChromeClient {
        @Override
        public void onProgressChanged(WebView view, int value) {
            progress.setProgress(value);
            progress.setVisibility(value < 100 ? View.VISIBLE : View.GONE);
        }

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            boolean mic = false;
            for (String r : request.getResources()) mic |= PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r);
            if (!mic || !isServer(request.getOrigin())) {
                request.deny();
                return;
            }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                return;
            }
            if (pendingMic != null) pendingMic.deny();
            pendingMic = request;
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }

        @Override
        public void onPermissionRequestCanceled(PermissionRequest request) {
            if (request == pendingMic) pendingMic = null;
        }

        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = callback;
            // CAMERA est déclarée (lecture du QR code) : Android l'exige alors aussi pour l'appareil photo du système
            if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                afterCamera = () -> chooseFile(params);
                requestPermissions(new String[] {Manifest.permission.CAMERA}, REQ_CAMERA);
            } else {
                chooseFile(params);
            }
            return true;
        }
    }

    private void chooseFile(WebChromeClient.FileChooserParams params) {
        boolean images = false, others = false;
        for (String accept : params.getAcceptTypes()) {
            if (accept == null) continue;
            for (String type : accept.split(",")) {
                type = type.trim();
                if (type.startsWith("image/")) images = true;
                else if (!type.isEmpty()) others = true;
            }
        }
        if (!images && !others) images = true;             // accept vide : tout, photo comprise
        Intent pick = new Intent(Intent.ACTION_GET_CONTENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType(images && !others ? "image/*" : "*/*")
                .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE);
        Intent camera = images ? cameraIntent() : null;
        Intent intent;
        if (params.isCaptureEnabled() && camera != null) {
            intent = camera;                                // bouton « Photo » du chat (capture="environment")
        } else {
            intent = Intent.createChooser(pick, "Joindre un fichier");
            if (camera != null) intent.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[] {camera});
        }
        try {
            startActivityForResult(intent, REQ_FILE);
        } catch (ActivityNotFoundException e) {
            fileCallback.onReceiveValue(null);
            fileCallback = null;
        }
    }

    private Intent cameraIntent() {
        Intent i = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED
                || i.resolveActivity(getPackageManager()) == null) return null;
        photoUri = PhotoProvider.newPhoto(this);
        i.putExtra(MediaStore.EXTRA_OUTPUT, photoUri);
        i.setClipData(ClipData.newRawUri("", photoUri));
        i.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        return i;
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request == REQ_SCAN) {
            String text = result == RESULT_OK && data != null ? data.getStringExtra(ScanActivity.EXTRA_TEXT) : null;
            if (text == null) return;
            connect(text.trim());                        // lien <adresse>/#key=… du PC : vérifié comme une saisie
        } else if (request == REQ_FILE && fileCallback != null) {
            List<Uri> uris = new ArrayList<>();
            if (result == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    for (int i = 0; i < data.getClipData().getItemCount(); i++) {
                        Uri u = data.getClipData().getItemAt(i).getUri();
                        if (u != null && !u.equals(photoUri)) uris.add(u);
                    }
                }
                if (uris.isEmpty() && data.getData() != null) uris.add(data.getData());
            }
            if (result == RESULT_OK && uris.isEmpty() && photoUri != null && PhotoProvider.hasContent(this, photoUri)) {
                uris.add(photoUri);
            }
            fileCallback.onReceiveValue(uris.isEmpty() ? null : uris.toArray(new Uri[0]));
            fileCallback = null;
            photoUri = null;
        } else if (request == REQ_SAVE) {
            byte[] bytes = pendingSave;
            pendingSave = null;
            if (result != RESULT_OK || data == null || data.getData() == null || bytes == null) return;
            Uri target = data.getData();
            io.execute(() -> {
                String message = "Fichier enregistré.";
                try (OutputStream out = getContentResolver().openOutputStream(target)) {
                    out.write(bytes);
                } catch (IOException | NullPointerException e) {
                    message = "Enregistrement impossible.";
                }
                String text = message;
                runOnUiThread(() -> Toast.makeText(this, text, Toast.LENGTH_SHORT).show());
            });
        } else {
            super.onActivityResult(request, result, data);
        }
    }

    @Override
    public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        if (request == REQ_CAMERA) {                     // accordée ou non : le choix du fichier s'ouvre
            Runnable next = afterCamera;
            afterCamera = null;
            if (next != null) next.run();
            return;
        }
        if (request != REQ_MIC || pendingMic == null) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            pendingMic.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            pendingMic.deny();
            Toast.makeText(this, "Micro refusé : autorisez-le dans les réglages de l'application pour la dictée.",
                    Toast.LENGTH_LONG).show();
        }
        pendingMic = null;
    }

    // ---------- pont JavaScript (window.LezardApp) ----------

    private class Bridge {
        /** Export, clé de chiffrement… (static/save-file.js) : la personne choisit où l'enregistrer. */
        @JavascriptInterface
        public void saveFile(String name, String mime, String base64) {
            byte[] bytes;
            try {
                bytes = Base64.decode(base64, Base64.DEFAULT);
            } catch (IllegalArgumentException e) {
                return;
            }
            runOnUiThread(() -> {
                if (!onServerPage()) return;
                pendingSave = bytes;
                Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT)
                        .addCategory(Intent.CATEGORY_OPENABLE)
                        .setType(mime == null || mime.isEmpty() ? "application/octet-stream" : mime.split(";")[0])
                        .putExtra(Intent.EXTRA_TITLE, name);
                try {
                    startActivityForResult(i, REQ_SAVE);
                } catch (ActivityNotFoundException e) {
                    pendingSave = null;
                    Toast.makeText(MainActivity.this, "Enregistrement impossible.", Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public void copyText(String text) {
            runOnUiThread(() -> {
                if (!onServerPage()) return;
                ((ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE))
                        .setPrimaryClip(ClipData.newPlainText("Lezard IA", text));
                if (Build.VERSION.SDK_INT < 33) {          // Android 13+ affiche déjà sa propre confirmation
                    Toast.makeText(MainActivity.this, "Copié", Toast.LENGTH_SHORT).show();
                }
            });
        }

        /** Bouton « Changer de serveur » de la page de connexion (login.js). */
        @JavascriptInterface
        public void changeServer() {
            runOnUiThread(() -> {
                if (onServerPage()) showSetup();
            });
        }
    }

    // ---------- cycle de vie ----------

    @Override
    public void onBackPressed() {
        if (setup.getVisibility() == View.VISIBLE && server != null) setup.setVisibility(View.GONE);
        else if (errorPanel.getVisibility() != View.VISIBLE && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        web.onPause();
        CookieManager.getInstance().flush();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        io.shutdownNow();
        web.destroy();
        super.onDestroy();
    }

    private String versionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "?";
        }
    }
}
