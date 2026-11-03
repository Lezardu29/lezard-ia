package fr.lezardia.app;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * Photos prises pour une pièce jointe du chat : l'application appareil photo écrit dans un fichier du cache de
 * l'application, que la WebView lit ensuite. Seuls les noms « photo-<horodatage>.jpg » sont servis ; les photos de
 * plus d'un jour sont effacées à la prise suivante.
 */
public class PhotoProvider extends ContentProvider {
    static final String AUTHORITY = "fr.lezardia.app.photos";
    private static final long MAX_AGE_MS = 24L * 3600 * 1000;

    static Uri newPhoto(Context context) {
        File dir = dir(context);
        File[] old = dir.listFiles();
        if (old != null) {
            long limit = System.currentTimeMillis() - MAX_AGE_MS;
            for (File f : old) if (f.lastModified() < limit) f.delete();
        }
        return Uri.parse("content://" + AUTHORITY + "/photo-" + System.currentTimeMillis() + ".jpg");
    }

    static boolean hasContent(Context context, Uri uri) {
        File f = file(context, uri);
        return f != null && f.length() > 0;
    }

    private static File dir(Context context) {
        File dir = new File(context.getCacheDir(), "photos");
        dir.mkdirs();
        return dir;
    }

    private static File file(Context context, Uri uri) {
        String name = uri.getLastPathSegment();
        if (!AUTHORITY.equals(uri.getAuthority()) || name == null || !name.matches("photo-\\d+\\.jpg")) return null;
        return new File(dir(context), name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = file(getContext(), uri);
        if (f == null) throw new FileNotFoundException(uri.toString());
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.parseMode(mode));
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sortOrder) {
        File f = file(getContext(), uri);
        if (f == null) return null;
        String[] columns = projection != null ? projection
                : new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        Object[] row = new Object[columns.length];
        for (int i = 0; i < columns.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(columns[i])) row[i] = f.getName();
            else if (OpenableColumns.SIZE.equals(columns[i])) row[i] = f.length();
        }
        MatrixCursor cursor = new MatrixCursor(columns, 1);
        cursor.addRow(row);
        return cursor;
    }

    @Override
    public String getType(Uri uri) {
        return "image/jpeg";
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] args) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] args) {
        return 0;
    }
}
