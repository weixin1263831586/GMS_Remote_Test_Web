package com.gms.remote.aoacalibration;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;

/** Probe touches are consumed by this empty surface and cannot change Settings. */
public final class CalibrationActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_FULLSCREEN);
        setContentView(new View(this) {
            private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
            private float x = -1, y = -1;
            @Override public boolean onTouchEvent(MotionEvent event) {
                x = event.getX(); y = event.getY(); invalidate(); return true;
            }
            @Override protected void onDraw(Canvas canvas) {
                canvas.drawColor(Color.rgb(20, 24, 32));
                paint.setColor(Color.WHITE); paint.setTextSize(26);
                canvas.drawText("AOA Calibration - safe touch surface", 20, 50, paint);
                if (x >= 0) {
                    paint.setColor(Color.GREEN);
                    canvas.drawCircle(x, y, 12, paint);
                }
            }
        });
    }
}
