import os
import sys
import shutil
import zipfile
import hashlib

base_dir = r"f:\Works\chat_bot\_ref_mkt_zalo"
out_dir = os.path.join(base_dir, "release_builds_v1.0.0")
os.makedirs(out_dir, exist_ok=True)

exe_path = os.path.join(base_dir, "dist", "Jimmy Chat Bot.exe")
version_path = os.path.join(base_dir, "VERSION")
installer_path = os.path.join(base_dir, "installer_output", "Jimmy_Chat_Bot_Setup_v1.0.0.exe")

readme_txt = os.path.join(out_dir, "Huong_Dan_Su_Dung.txt")
with open(readme_txt, "w", encoding="utf-8") as f:
    f.write(
        "=== JIMMY CHAT BOT v1.0.0 - HUONG DAN SU DUNG ===\n\n"
        "1. Chay file 'Jimmy Chat Bot.exe' de khoi dong ung dung.\n"
        "2. Neu bi bao boi Windows Defender / SmartScreen, bam 'More info' -> 'Run anyway'.\n"
        "3. Dang nhap tai khoan Zalo va kich hoat ban quyen de su dung.\n"
        "4. Chuc ban thanh cong!\n"
    )

gdrive_zip_path = os.path.join(out_dir, "Jimmy_Chat_Bot_v1.0.0_Portable.zip")
print("Dang nen Jimmy_Chat_Bot_v1.0.0_Portable.zip...")
with zipfile.ZipFile(gdrive_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
    z.write(exe_path, "Jimmy Chat Bot.exe")
    z.write(version_path, "VERSION")
    z.write(readme_txt, "Huong_Dan_Su_Dung.txt")
    for folder in ["templates", "static"]:
        src_folder = os.path.join(base_dir, folder)
        for root, dirs, files in os.walk(src_folder):
            for file in files:
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, base_dir)
                z.write(full_p, rel_p)

update_zip_path = os.path.join(out_dir, "Jimmy_Chat_Bot.zip")
print("Dang nen Jimmy_Chat_Bot.zip cho updater...")
with zipfile.ZipFile(update_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
    z.write(exe_path, "Jimmy Chat Bot.exe")
    z.write(version_path, "VERSION")
    for folder in ["templates", "static"]:
        src_folder = os.path.join(base_dir, folder)
        for root, dirs, files in os.walk(src_folder):
            for file in files:
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, base_dir)
                z.write(full_p, rel_p)

hasher = hashlib.sha256()
with open(update_zip_path, "rb") as f:
    while chunk := f.read(65536):
        hasher.update(chunk)
sha256_hex = hasher.hexdigest()
sha256_path = os.path.join(out_dir, "Jimmy_Chat_Bot.zip.sha256")
with open(sha256_path, "w", encoding="utf-8") as f:
    f.write(sha256_hex + "\n")
print(f"SHA256: {sha256_hex}")

target_installer = os.path.join(out_dir, "Jimmy_Chat_Bot_Setup_v1.0.0.exe")
shutil.copy2(installer_path, target_installer)

print("HOAN TAT DONG GOI!")
