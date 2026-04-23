import pyaudio

p = pyaudio.PyAudio()

print(f"{'Index':<7} | {'Name':<50} | {'In':<4} | {'Out':<4}")
print("-" * 70)

for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    name = dev.get('name')
    # Kürzen, falls der Name zu lang für die Tabelle ist
    display_name = (name[:47] + '...') if len(name) > 50 else name
    
    in_channels = dev.get('maxInputChannels')
    out_channels = dev.get('maxOutputChannels')
    
    print(f"{i:<7} | {display_name:<50} | {in_channels:<4} | {out_channels:<4}")

p.terminate()