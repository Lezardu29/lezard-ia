package fr.lezardia.app;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.EncodeHintType;
import com.google.zxing.common.BitMatrix;
import com.google.zxing.qrcode.QRCodeWriter;
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel;

import java.awt.image.BufferedImage;
import java.io.File;
import java.util.EnumMap;
import java.util.Map;
import java.util.Random;

import javax.imageio.ImageIO;

/**
 * Vérification de QrDecoder sur le PC (lancée par android/build.py) : QR codes comme ceux de « Connecter un
 * téléphone » (lien <adresse>/#key=…, correction M comme le paquet Python qrcode), vus comme par une caméra :
 * petits au milieu d'une image 1280x720, tournés, flous, peu contrastés, bruités. Arguments : images réelles
 * (captures du panneau) à décoder en plus. Code de sortie 1 au premier échec.
 */
public final class QrDecoderCheck {
    private static final int W = 1280, H = 720;
    private static int failures;

    public static void main(String[] args) throws Exception {
        String link = "https://ia.votre-domaine.fr/#key=sk-app-" + "x7Kp2QmZ9vT4bN8rL1sD6fH3jW0yC5aE";
        byte[][] qr = render(link, 300);
        check("droit", link, place(qr, 0, 1, 0, 0));
        check("tourné 90°", link, place(qr, 1, 1, 0, 0));
        check("tourné 180°", link, place(qr, 2, 1, 0, 0));
        check("petit (loin)", link, place(render(link, 170), 0, 1, 0, 0));
        check("flou", link, place(qr, 1, 3, 0, 0));
        check("peu contrasté", link, place(qr, 0, 1, 70, 0));
        check("bruité", link, place(qr, 3, 1, 0, 25));
        check("lien réseau local", "http://192.168.1.20:8000/#key=sk-" + "a".repeat(40),
                place(render("http://192.168.1.20:8000/#key=sk-" + "a".repeat(40), 300), 0, 2, 40, 15));
        byte[] empty = new byte[W * H];
        java.util.Arrays.fill(empty, (byte) 128);
        if (new QrDecoder().decode(empty, W, H) != null) fail("image vide : un texte a été inventé");
        else System.out.println("ok  image vide : rien");
        for (String path : args) {
            BufferedImage img = ImageIO.read(new File(path));
            byte[] y = new byte[img.getWidth() * img.getHeight()];
            for (int j = 0; j < img.getHeight(); j++) {
                for (int i = 0; i < img.getWidth(); i++) {
                    int rgb = img.getRGB(i, j);
                    y[j * img.getWidth() + i] = (byte) ((((rgb >> 16) & 255) * 299 + ((rgb >> 8) & 255) * 587
                            + (rgb & 255) * 114) / 1000);
                }
            }
            String text = new QrDecoder().decode(y, img.getWidth(), img.getHeight());
            if (text == null) fail(path + " : illisible");
            else System.out.println("ok  " + path + " : " + text.replaceAll("#key=.*", "#key=…"));
        }
        if (failures > 0) System.exit(1);
    }

    /** QR en pixels (0 = noir, 255 = blanc), marge de 4 modules, côté d'environ `size` pixels. */
    private static byte[][] render(String text, int size) throws Exception {
        Map<EncodeHintType, Object> hints = new EnumMap<>(EncodeHintType.class);
        hints.put(EncodeHintType.ERROR_CORRECTION, ErrorCorrectionLevel.M);
        hints.put(EncodeHintType.MARGIN, 4);
        BitMatrix m = new QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size, hints);
        byte[][] px = new byte[m.getHeight()][m.getWidth()];
        for (int j = 0; j < m.getHeight(); j++) {
            for (int i = 0; i < m.getWidth(); i++) px[j][i] = (byte) (m.get(i, j) ? 0 : 255);
        }
        return px;
    }

    /** Image caméra W x H (fond gris) avec le QR au centre : quarts de tour, flou (rayon), contraste réduit, bruit. */
    private static byte[] place(byte[][] qr, int quarterTurns, int blur, int contrastLoss, int noise) {
        int n = qr.length;
        byte[] img = new byte[W * H];
        java.util.Arrays.fill(img, (byte) 110);
        int x0 = (W - n) / 2, y0 = (H - n) / 2;
        for (int j = 0; j < n; j++) {
            for (int i = 0; i < n; i++) {
                int si = i, sj = j;
                for (int t = 0; t < quarterTurns; t++) { int tmp = si; si = n - 1 - sj; sj = tmp; }
                int v = qr[sj][si] & 255;
                v = contrastLoss + v * (255 - 2 * contrastLoss) / 255;
                img[(y0 + j) * W + x0 + i] = (byte) v;
            }
        }
        if (blur > 1) img = boxBlur(img, blur);
        Random random = new Random(42);
        for (int k = 0; k < img.length && noise > 0; k++) {
            int v = (img[k] & 255) + random.nextInt(2 * noise + 1) - noise;
            img[k] = (byte) Math.max(0, Math.min(255, v));
        }
        return img;
    }

    private static byte[] boxBlur(byte[] src, int r) {
        byte[] out = new byte[src.length];
        for (int y = 0; y < H; y++) {
            for (int x = 0; x < W; x++) {
                int sum = 0, count = 0;
                for (int dy = -r; dy <= r; dy++) {
                    for (int dx = -r; dx <= r; dx++) {
                        int xx = x + dx, yy = y + dy;
                        if (xx >= 0 && yy >= 0 && xx < W && yy < H) { sum += src[yy * W + xx] & 255; count++; }
                    }
                }
                out[y * W + x] = (byte) (sum / count);
            }
        }
        return out;
    }

    private static void check(String name, String expected, byte[] img) {
        String text = new QrDecoder().decode(img, W, H);
        if (expected.equals(text)) System.out.println("ok  " + name);
        else fail(name + " : lu " + text);
    }

    private static void fail(String message) {
        failures++;
        System.out.println("ÉCHEC  " + message);
    }
}
