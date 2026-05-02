# Keep WebView JS bridge names if added later
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
