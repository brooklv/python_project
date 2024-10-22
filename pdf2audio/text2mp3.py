from gtts import gTTS
import os
import platform

def text_to_speech(text, language='en', output_file='output.mp3'):
    tts = gTTS(text=text, lang=language)
    tts.save(output_file)
    
    # Play the audio file
    if platform.system() == "Windows":
        os.startfile(output_file)  # Use start for Windows
    elif platform.system() == "Darwin":  # macOS
        os.system(f"open {output_file}")
    else:  # Assume Linux
        os.system(f"xdg-open {output_file}")

if __name__ == "__main__":
    sample_text = "Hello, this is a text to speech conversion example."
    text_to_speech(sample_text)

