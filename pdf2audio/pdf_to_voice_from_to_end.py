import PyPDF2
from gtts import gTTS
import os
import platform

def extract_text_from_pdf(pdf_path, start_page=0, end_page=None):
    text = ""
    with open(pdf_path, "rb") as file:
        reader = PyPDF2.PdfReader(file)
        total_pages = len(reader.pages)

        # If end_page is not specified, set it to the last page
        if end_page is None or end_page > total_pages:
            end_page = total_pages
        
        # Loop through the specified page range
        for page_number in range(start_page, end_page):
            if page_number < total_pages:
                text += reader.pages[page_number].extract_text() or ""  # Handle None if no text found
                # Calculate and display progress percentage
                progress = ((page_number - start_page + 1) / (end_page - start_page)) * 100
                print(f"Extracting page {page_number + 1}/{end_page}: {progress:.2f}% complete")
    return text

def save_text_to_file(text, start_page, end_page):
    # Save the extracted text to a file named based on the page range
    filename = f'pages_{start_page}_to_{end_page}.txt'
    with open(filename, 'w') as text_file:
        text_file.write(text)
    print(f"Extracted text saved to {filename}")

def text_to_speech(text, language='en', output_file='output.mp3'):
    print("start gTTS")
    tts = gTTS(text=text, lang=language)
    print("gTTS end and start to save")
    tts.save(output_file)
    print("save end")
    
    # Play the audio file
    if platform.system() == "Windows":
        os.startfile(output_file)
    elif platform.system() == "Darwin":
        os.system(f"open {output_file}")
    else:
        os.system(f"xdg-open {output_file}")

if __name__ == "__main__":
    pdf_path = 'yourfile.pdf'  # Replace with your actual PDF file path
    start_page = 0  # Replace with the starting page (0-indexed)
    end_page = 65    # Replace with the ending page (exclusive)

    # Create a dynamic output file name based on the end page
    output_file = f'output_page_{end_page}.mp3'

    extracted_text = extract_text_from_pdf(pdf_path, start_page, end_page)
    
    if extracted_text:
        save_text_to_file(extracted_text, start_page, end_page)
        text_to_speech(extracted_text, output_file=output_file)
    else:
        print("No text found in the specified PDF pages.")
