import sounddevice as sd
import soundfile as sf
import numpy as np
import os
import requests
import time

# --- KONFIGURATION ---
SAMPLERATE = 16000
DURATION = 10  # 10 Sekunden sind ideal für ein stabiles Stimmprofil
PI_URL = "http://192.168.8.2:8080/enroll"  # IP deines Pi 5 einsetzen
ROOMIE_ID = "leonie"  # Dein Name für die Stimm-ID

# Ein Text, der viele verschiedene Laute abdeckt
ENROLL_TEXT = """
"Hannah, ich möchte, dass du dir meine Stimme merkst. 
Das Wetter heute ist schön und ich freue mich darauf, 
dass du mich in Zukunft sofort erkennst, wenn ich den Raum betrete."
"""

def list_devices():
    print("Verfügbare Mikrofon-Geräte:")
    for i, dev in enumerate(sd.query_devices()):
        if dev['max_input_channels'] > 0:
            print(f"  [{i}] {dev['name']}")

def record_audio(mic_id):
    print(f"\nBereit? Bitte lies den folgenden Text deutlich vor:\n")
    print("-" * 50)
    print(ENROLL_TEXT)
    print("-" * 50)
    input("\nDrücke ENTER, um die 10-sekündige Aufnahme zu STARTEN...")
    
    print("\n🔴 AUFNAHME LÄUFT...")
    audio = sd.rec(int(DURATION * SAMPLERATE), samplerate=SAMPLERATE, channels=1, dtype='int16', device=mic_id)
    
    # Kleiner Fortschrittsbalken
    for i in range(DURATION):
        time.sleep(1)
        print(f"Noch {DURATION - i - 1} Sekunden...")
        
    sd.wait()
    print("✅ Aufnahme beendet.")
    return audio

def save_and_send(audio, roomie_id):
    filename = f"enroll_{roomie_id}.wav"
    # Lokal speichern
    sf.write(filename, audio, SAMPLERATE)
    print(f"Lokal gespeichert als: {filename}")
    
    # An den Pi senden
    try:
        with open(filename, 'rb') as f:
            audio_bytes = f.read()
            
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Sample-Rate": str(SAMPLERATE),
            "X-Roomie-ID": roomie_id
        }
        
        print(f"Sende Daten an Hannah Identity Service ({PI_URL})...")
        response = requests.post(PI_URL, data=audio_bytes, headers=headers, timeout=10)
        
        if response.status_code == 200:
            print("🚀 Hannah hat deine Stimme erfolgreich gelernt!")
        else:
            print(f"❌ Fehler vom Server: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"❌ Verbindung zum Pi fehlgeschlagen: {e}")

def main():
    list_devices()
    try:
        mic_id = int(input("\nWähle die Mikrofon-ID: "))
        audio = record_audio(mic_id)
        
        confirm = input("\nBist du mit der Aufnahme zufrieden? (j/n): ")
        if confirm.lower() == 'j':
            save_and_send(audio, ROOMIE_ID)
        else:
            print("Abgebrochen. Starte das Script einfach neu.")
            
    except ValueError:
        print("Ungültige Eingabe.")

if __name__ == "__main__":
    main()