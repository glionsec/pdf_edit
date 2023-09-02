import os
from PyPDF2 import PdfReader, PdfWriter

# カレントディレクトリ内のPDFファイルをリストアップ
pdf_files = [file for file in os.listdir() if file.endswith('.pdf')]

# パスワードを入力してPDFファイルを処理
for pdf_file in pdf_files:
    print(f'処理中: {pdf_file}')
    
    # パスワードをユーザーに入力して取得
    password = input('PDFのパスワードを入力してください: ')
    
    try:
        # パスワードで保護されたPDFを開く
        pdf_reader = PdfReader(pdf_file)
        pdf_reader.decrypt(password)
        
        # 新しいPDFファイルを作成
        output_file = pdf_file[:-4] + '_ja.pdf'  # 末尾に"_ja"を付与
        pdf_writer = PdfWriter()
        
        # すべてのページを新しいPDFに追加
        for page in pdf_reader.pages:
            pdf_writer.add_page(page)
        
        # 新しいPDFファイルを保存
        with open(output_file, 'wb') as output_pdf:
            pdf_writer.write(output_pdf)
        
        print(f'処理完了: {output_file}')
    except Exception as e:
        print(f'エラー: {e}')

print('処理が完了しました。')
