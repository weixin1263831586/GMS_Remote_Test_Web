#!/usr/bin/env python3
"""Build the touch-safe calibration APK with local Android SDK tools, offline."""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--platform", default="android-35")
    parser.add_argument("--build-tools", default="35.0.0")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = find_repo_root()
    source = root / "worker_agent/calibration_app/src/main"
    sdk = args.sdk_root.resolve()
    android = sdk / "platforms" / args.platform / "android.jar"
    build_tools = sdk / "build-tools" / args.build_tools
    output = (args.output or root / "dist/aoa-calibration-debug.apk").resolve()
    if output.exists():
        raise FileExistsError(f"Build output already exists; choose a new --output: {output}")
    for path in (android, *(build_tools / name for name in ("aapt2", "d8", "zipalign", "apksigner"))):
        if not path.is_file():
            raise FileNotFoundError(path)
    for command in ("javac", "keytool"):
        if not shutil.which(command):
            raise RuntimeError(f"JDK is required: missing {command}")
    env = {**os.environ, "GMS_AOA_DEBUG_KS_PASSWORD": secrets.token_hex(16)}
    def run(*argv):
        result = subprocess.run([str(arg) for arg in argv], env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"{Path(argv[0]).name}: {(result.stderr.strip() or result.stdout.strip())[-2000:]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gms-aoa-apk-") as directory:
        temporary = Path(directory)
        classes, dex = temporary / "classes", temporary / "dex"
        classes.mkdir()
        dex.mkdir()
        # 可信来源豁免（nosemgrep: use-defused-xml-parse）：解析的是仓库内
        # 固定签入的 AndroidManifest.xml，无运行时外部输入，仅改写 package
        # 属性后交给 aapt2；引入 defusedxml 依赖对离线构建脚本没有增量收益。
        manifest = ET.parse(source / "AndroidManifest.xml")  # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
        manifest.getroot().set("package", "com.gms.remote.aoacalibration")
        manifest.write(temporary / "AndroidManifest.xml", encoding="utf-8", xml_declaration=True)
        run("javac", "--release", "8", "-classpath", android, "-d", classes,
            *sorted((source / "java").rglob("*.java")))
        run(build_tools / "d8", "--lib", android, "--min-api", "23", "--output", dex,
            *sorted(classes.rglob("*.class")))
        unsigned = temporary / "unsigned.apk"
        run(build_tools / "aapt2", "link", "--manifest", temporary / "AndroidManifest.xml",
            "-I", android, "-o", unsigned, "--min-sdk-version", "23", "--target-sdk-version", "28")
        with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(dex / "classes.dex", "classes.dex")
        aligned = temporary / "aligned.apk"
        run(build_tools / "zipalign", "4", unsigned, aligned)
        key = temporary / "debug.jks"
        run("keytool", "-genkeypair", "-keystore", key, "-alias", "aoa-debug",
            "-storepass:env", "GMS_AOA_DEBUG_KS_PASSWORD", "-keypass:env", "GMS_AOA_DEBUG_KS_PASSWORD",
            "-keyalg", "RSA", "-validity", "3650", "-dname", "CN=AOA Development", "-noprompt")
        signed = temporary / "signed.apk"
        run(build_tools / "apksigner", "sign", "--ks", key, "--ks-key-alias", "aoa-debug",
            "--ks-pass", "env:GMS_AOA_DEBUG_KS_PASSWORD", "--out", signed, aligned)
        run(build_tools / "apksigner", "verify", signed)
        shutil.copyfile(signed, output)
    print(f"Built and signature-verified: {output}")


if __name__ == "__main__":
    main()
