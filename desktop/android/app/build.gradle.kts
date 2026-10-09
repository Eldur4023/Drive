plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.lux.local"
    compileSdk = 34
    defaultConfig {
        applicationId = "dev.lux.drive"   // the Kotlin package stays dev.lux.local: JNI symbols (src/android.cpp) are named after it
        minSdk = 28          // bionic's posix_spawn, which Lux's os/proc modules use
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }
    // libluxlocal.so is built by ../tools/android-build.sh into src/main/jniLibs/<abi>/
    kotlinOptions { jvmTarget = "17" }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
}
