from flask import Flask, send_from_directory, jsonify, request, Response
import subprocess
import os
import zipfile
import uuid
import shutil
import threading
import time
import re
from urllib.parse import quote

app = Flask(__name__, static_folder='web')
BASE_DOWNLOAD_FOLDER = '/app/downloads'
AUDIO_DOWNLOAD_PATH = os.getenv('AUDIO_DOWNLOAD_PATH', BASE_DOWNLOAD_FOLDER)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'password')
CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', 3600))  # Default 1 hour

sessions = {}

os.makedirs(BASE_DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(AUDIO_DOWNLOAD_PATH, exist_ok=True)

@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(app.static_folder, path)

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session_id = str(uuid.uuid4())
        sessions[session_id] = username
        response = jsonify({"success": True})
        response.set_cookie('session', session_id)
        return response
    return jsonify({"success": False}), 401

def is_logged_in():
    session_id = request.cookies.get('session')
    return session_id in sessions

@app.route('/logout', methods=['POST'])
def logout():
    session_id = request.cookies.get('session')
    if session_id in sessions:
        del sessions[session_id]
    response = jsonify({"success": True})
    response.delete_cookie('session')
    return response

@app.route('/check-login')
def check_login():
    return jsonify({"loggedIn": is_logged_in()})

@app.route('/download')
def download_media():
    spotify_link = request.args.get('spotify_link')
    if not spotify_link:
        return jsonify({"status": "error", "output": "No link provided"}), 400

    session_id = str(uuid.uuid4())
    download_folder = AUDIO_DOWNLOAD_PATH if is_logged_in() else os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    os.makedirs(download_folder, exist_ok=True)

    if "spotify" in spotify_link:
        command = [
            'spotdl',
            '--output', f"{download_folder}/{{artist}}/{{album}}/{{track-number}} - {{title}}.{{output-ext}}",
            spotify_link
        ]
    else:
        command = [
            'yt-dlp', '-x', '--audio-format', 'mp3',
            '-o', f"{download_folder}/%(uploader|artist)s/%(album|playlist)s/%(playlist_index)s - %(title)s.%(ext)s",
            spotify_link
        ]

    is_admin = is_logged_in()
    return Response(generate(is_admin, command, download_folder, session_id), mimetype='text/event-stream')

def generate(is_admin, command, download_folder, session_id):
    album_name = "playlist"
    try:
        print(f"🎧 Command: {' '.join(command)}")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_output_time = time.time()

        while process.poll() is None:  # While process is running
            line = process.stdout.readline()
            if line:
                print(f"▶️ {line.strip()}")
                yield f"data: {line.strip()}\n\n"
                last_output_time = time.time()
                # Extract album/playlist name
                match = re.search(r'Found \d+ songs in (.+?) \(', line) or re.search(r'Downloading playlist "(.+?)"', line)
                if match:
                    album_name = match.group(1).strip()
            elif time.time() - last_output_time > 300:  # 5-minute timeout
                process.kill()
                yield f"data: Error: Download stalled for 5 minutes.\n\n"
                break

        # Drain remaining output
        for line in process.stdout:
            print(f"▶️ {line.strip()}")
            yield f"data: {line.strip()}\n\n"

        if process.returncode == 0:
            downloaded_files = [os.path.join(root, f) for root, _, files in os.walk(download_folder) for f in files]
            valid_audio_files = [f for f in downloaded_files if f.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg'))]

            if not valid_audio_files:
                yield f"data: Error: No valid audio files found.\n\n"
                return

            # Post-process YouTube " - topic" folders
            if not "spotify" in command and is_admin:
                for root, dirs, _ in os.walk(download_folder):
                    for d in dirs:
                        if d.endswith(" - topic"):
                            new_name = d.replace(" - topic", "")
                            os.rename(os.path.join(root, d), os.path.join(root, new_name))
                            print(f"Renamed folder: {d} -> {new_name}")

            if len(valid_audio_files) > 1 and not is_admin:
                zip_filename = f"{album_name}.zip"
                zip_path = os.path.join(download_folder, zip_filename)
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for file in valid_audio_files:
                        arcname = os.path.relpath(file, download_folder)
                        zipf.write(file, arcname)
                        print(f"📦 Added to zip: {arcname}")
                yield f"data: DOWNLOAD: {session_id}/{zip_filename}\n\n"
            elif valid_audio_files and not is_admin:
                relative_path = os.path.relpath(valid_audio_files[0], download_folder)
                yield f"data: DOWNLOAD: {session_id}/{quote(relative_path)}\n\n"
            else:
                yield "data: Download completed. Files saved to server directory.\n\n"
                yield "event: complete\ndata: done\n\n"  # Signal end for admin mode

            if not is_admin:
                threading.Thread(target=delayed_delete, args=(download_folder,)).start()
        else:
            yield f"data: Error: Download exited with code {process.returncode}.\n\n"
    except Exception as e:
        yield f"data: Error: {str(e)}\n\n"

def delayed_delete(folder_path):
    time.sleep(300)
    shutil.rmtree(folder_path, ignore_errors=True)

def emergency_cleanup_container_downloads():
    print("🚨 Running cleanup in /app/downloads")
    for folder in os.listdir(BASE_DOWNLOAD_FOLDER):
        try:
            shutil.rmtree(os.path.join(BASE_DOWNLOAD_FOLDER, folder))
            print(f"🗑️ Cleaned: {folder}")
        except Exception as e:
            print(f"⚠️ Could not delete {folder}: {e}")

def schedule_emergency_cleanup():
    threading.Thread(target=lambda: [time.sleep(CLEANUP_INTERVAL) or emergency_cleanup_container_downloads() for _ in iter(int, 1)], daemon=True).start()

@app.route('/downloads/<session_id>/<path:filename>')
def serve_download(session_id, filename):
    session_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    full_path = os.path.join(session_download_folder, filename)
    if ".." in filename or filename.startswith("/"):
        return "Invalid filename", 400
    if not os.path.isfile(full_path):
        return "File not found", 404
    return send_from_directory(session_download_folder, filename, as_attachment=True)

schedule_emergency_cleanup()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
