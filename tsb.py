import os
import sys
import hashlib
import threading
import socket
import time
import shutil
import zipfile
from pathlib import Path
from flask import Flask, request, jsonify
from pyngrok import ngrok, conf, installer
import logging

# Ебанные логи выключены
log = logging.getLogger('werkzeug')
log.disabled = True
logging.getLogger('flask').disabled = True
logging.basicConfig(level=logging.CRITICAL)

# Основные константы
TOKEN_FILE = Path.home() / ".tsb_ngrok_token"
RECEIVE_FOLDER = Path.home() / "Desktop" / "TSB_Received"
RECEIVE_FOLDER.mkdir(parents=True, exist_ok=True)
CHUNK_SIZE = 3 * 1024 * 1024

app = Flask("TSB Server")
_transfer_state = {
    "filename": None,
    "filesize": None,
    "filehash": None,
    "received_bytes": 0,
    "start_time": None,
    "completed": False,
    "error": None,
    "is_archive": False,
}


def save_token(token: str):
    TOKEN_FILE.write_text(token.strip())


def load_token():
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else None


def input_token():
    print("- Для работы с Ngrok необходим ваш Authtoken.")
    print("- Получить токен можно по ссылке: https://dashboard.ngrok.com/get-started/your-authtoken")
    token = input("Введите ваш Ngrok Authtoken: ").strip()
    save_token(token)
    return token


def ensure_ngrok_token():
    token = load_token()
    if not token:
        token = input_token()
    conf.get_default().auth_token = token

    try:
        ngrok.set_auth_token(token)
        test = ngrok.connect(4040)  # Если все ебанет
        ngrok.disconnect(test.public_url)
    except Exception:
        print("- Токен недействителен. Введите его заново.")
        token = input_token()
        conf.get_default().auth_token = token
        ngrok.set_auth_token(token)

    return token


def md5_checksum(file_path):
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5.update(chunk)
    return md5.hexdigest()


@app.route("/init", methods=["POST"])
def init_transfer():
    data = request.json
    if not data or "filename" not in data or "filesize" not in data or "filehash" not in data:
        return jsonify({"error": "Invalid init data"}), 400

    _transfer_state.update({
        "filename": data["filename"],
        "filesize": data["filesize"],
        "filehash": data["filehash"],
        "received_bytes": 0,
        "completed": False,
        "error": None,
        "start_time": time.time(),
        "is_archive": data["filename"].endswith(".tsbzip")
    })
    return jsonify({"status": "ready"})


@app.route("/send_chunk", methods=["POST"])
def receive_chunk():
    chunk = request.data
    if not chunk:
        return jsonify({"error": "No data received"}), 400

    file_path = RECEIVE_FOLDER / _transfer_state["filename"]
    with open(file_path, "ab") as f:
        f.write(chunk)
    _transfer_state["received_bytes"] += len(chunk)

    # Прогресс и измерение скорости
    elapsed = time.time() - _transfer_state["start_time"]
    speed = _transfer_state["received_bytes"] / elapsed if elapsed > 0 else 1
    remaining = _transfer_state["filesize"] - _transfer_state["received_bytes"]
    eta = remaining / speed if speed > 0 else 0
    print(f"📥 Получено {_transfer_state['received_bytes']}/{_transfer_state['filesize']} байт "
          f"(Осталось: {int(eta)} сек)", end="\r")

    if _transfer_state["received_bytes"] >= _transfer_state["filesize"]:
        file_hash = md5_checksum(file_path)
        if file_hash == _transfer_state["filehash"]:
            _transfer_state["completed"] = True
            print("\n✅ Файл успешно получен!")
            if _transfer_state["is_archive"]:
                try:
                    with zipfile.ZipFile(file_path, 'r') as zip_ref:
                        extract_path = RECEIVE_FOLDER / file_path.stem
                        extract_path.mkdir(parents=True, exist_ok=True)
                        zip_ref.extractall(extract_path)
                    os.remove(file_path)
                    print(f"- Архив распакован в {extract_path}")
                except Exception as e:
                    print(f"\n❗ Ошибка распаковки: {e}")
        else:
            _transfer_state["error"] = "Hash mismatch"
            print("\n❌ Хеш не совпадает!")

    return jsonify({"status": "chunk received"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(_transfer_state)


def find_free_port(start=8000, end=9000):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError("❌ Не удалось найти свободный порт")


def start_ngrok_tunnel(port):
    ensure_ngrok_token()
    tunnel = ngrok.connect(port)
    print(f"🌐 TSB URL: {tunnel.public_url}")
    return tunnel

def send_file(url, file_path):
    import requests

    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        print(f"❌ Файл не найден: {file_path}")
        return

    file_path = Path(file_path)
    if not file_path.suffix.lower() in [".zip", ".rar", ".7z", ".tsbzip"]:
        # Архивируем файл
        zipped_path = file_path.with_suffix(".tsbzip")
        with zipfile.ZipFile(zipped_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, arcname=file_path.name)
        file_path = zipped_path
        is_archive = True

    filesize = file_path.stat().st_size
    filehash = md5_checksum(file_path)
    filename = file_path.name

    print(f"- Отправка файла '{filename}' ({filesize} байт)")

    session = requests.Session()
    init_response = session.post(f"{url}/init", json={
        "filename": filename,
        "filesize": filesize,
        "filehash": filehash
    })
    if init_response.status_code != 200:
        print("❌ Ошибка инициализации:", init_response.text)
        return

    sent = 0
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            r = session.post(f"{url}/send_chunk", data=chunk)
            if r.status_code != 200:
                print("\n- Ошибка отправки:", r.text)
                return
            sent += len(chunk)
            print(f"- Отправлено {sent}/{filesize} байт", end="\r")

    status = session.get(f"{url}/status")
    if status.ok and status.json().get("completed"):
        print(f"\n✅ Отправка завершена. Файл успешно принят.")
    else:
        print("\n- Передача завершилась с ошибкой:", status.text)

    if is_archive:
        os.remove(file_path)


# Инуструкция
def print_usage():
    print("""
TSB — Tuneling for Share Bytes | Автор: Github.com/A01L

Использование:
  tsb receive
    Запускает прием файла. Отобразит ссылку для отправки.

  tsb send <file_path> <ngrok_url>
    Отправляет файл на указанный адрес.
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1].lower()

    if command == "receive":
        port = find_free_port()
        print(f"______ TSB (Tunneling for Share Bytes) | GIT Atuhor: A01L {port} ______")
        print(f"______ Запуск тунеля TSB на порту {port} ______")
        tunnel = start_ngrok_tunnel(port)
        print(f"Отправьте эту ссылку отправителю: {tunnel.public_url}")

        flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False), daemon=True)
        flask_thread.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🚪 Завершение...")
            ngrok.disconnect(tunnel.public_url)

    elif command == "send" and len(sys.argv) == 4:
        send_file(sys.argv[3], sys.argv[2])
    else:
        print_usage()


if __name__ == "__main__":
    main()
