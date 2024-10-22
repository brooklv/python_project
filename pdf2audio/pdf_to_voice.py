import PyPDF2
from gtts import gTTS
import os
import platform

def extract_text_from_pdf(pdf_path):
    text = ""
    with open(pdf_path, "rb") as file:
        reader = PyPDF2.PdfReader(file)
        for page in reader.pages:
            text += page.extract_text() or ""  # Handle None if no text found
    return text

def text_to_speech(text, language='en', output_file='output.mp3'):
    tts = gTTS(text=text, lang=language)
    tts.save(output_file)
    
    # Play the audio file
    if platform.system() == "Windows":
        os.startfile(output_file)
    elif platform.system() == "Darwin":
        os.system(f"open {output_file}")
    else:
        os.system(f"xdg-open {output_file}")

if __name__ == "__main__":
    pdf_path = 'yourfile.pdf'  # Replace with your actual PDF file path
    extracted_text = extract_text_from_pdf(pdf_path)
    
    if extracted_text:
        text_to_speech(extracted_text)
    else:
        print("No text found in the PDF.")

