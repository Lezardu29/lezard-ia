package fr.lezardia.app;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.ReaderException;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;

import java.util.Collections;
import java.util.EnumMap;
import java.util.Map;

/**
 * Décodage d'un QR code dans une image en niveaux de gris (plan Y de la caméra), avec ZXing. Sans dépendance à
 * Android : vérifiable sur le PC (android/tests/QrDecoderCheck.java). Le QR est lu quelle que soit son orientation.
 */
final class QrDecoder {
    private final QRCodeReader reader = new QRCodeReader();
    private final Map<DecodeHintType, Object> hints = new EnumMap<>(DecodeHintType.class);

    QrDecoder() {
        hints.put(DecodeHintType.POSSIBLE_FORMATS, Collections.singletonList(BarcodeFormat.QR_CODE));
        hints.put(DecodeHintType.TRY_HARDER, Boolean.TRUE);
    }

    /** Texte du QR code, ou null s'il n'y en a pas de lisible dans l'image. */
    String decode(byte[] luminance, int width, int height) {
        try {
            return reader.decode(new BinaryBitmap(new HybridBinarizer(
                    new PlanarYUVLuminanceSource(luminance, width, height, 0, 0, width, height, false))), hints).getText();
        } catch (ReaderException e) {
            return null;
        } finally {
            reader.reset();
        }
    }
}
