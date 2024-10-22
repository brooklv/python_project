import PyPDF2
import pyttsx3
import os
import platform

def extract_text_from_pdf(pdf_path, start_page=0, end_page=None):
    text = ""
    with open(pdf_path, "rb") as file:
        reader = PyPDF2.PdfReader(file)
        total_pages = len(reader.pages)

        if end_page is None or end_page > total_pages:
            end_page = total_pages
        
        for page_number in range(start_page, end_page):
            if page_number < total_pages:
                text += reader.pages[page_number].extract_text() or ""
                progress = ((page_number - start_page + 1) / (end_page - start_page)) * 100
                print(f"Extracting page {page_number + 1}/{end_page}: {progress:.2f}% complete")
    return text

def save_text_to_file(text, start_page, end_page):
    filename = f'pages_{start_page}_to_{end_page}.txt'
    with open(filename, 'w') as text_file:
        text_file.write(text)
    print(f"Extracted text saved to {filename}")

def text_to_speech(text, output_file='output.wav'):
    engine = pyttsx3.init()

    # Save the speech to a file
    engine.save_to_file(text, output_file)
    engine.runAndWait()
    print(f"Audio saved to {output_file}")

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

    output_file = f'output_page_{end_page}.wav'

    extracted_text = extract_text_from_pdf(pdf_path, start_page, end_page)
    
    if extracted_text:
        save_text_to_file(extracted_text, start_page, end_page)
        text_to_speech(extracted_text, output_file=output_file)
    else:
        print("No text found in the specified PDF pages.")
