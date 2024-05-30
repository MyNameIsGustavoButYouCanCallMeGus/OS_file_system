import os
import io
import tempfile
import logging
from googleapiclient.errors import HttpError
from flask import Flask, request, jsonify, render_template, send_file, redirect
from flask_sqlalchemy import SQLAlchemy
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sqlalchemy.orm import relationship
import hashlib

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:20041015R@localhost:5432/OS'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Google Drive API setup
SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = 'C:/Users/DTM/Desktop/-/OS/oscloud1-bbb4ff917bf0.json'  # Ensure the path is correct

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=credentials)

# Database model
class File(db.Model):
    __tablename__ = 'file'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    google_drive_id = db.Column(db.String(255), nullable=False)
    hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    versions = relationship('FileVersion', cascade='all, delete-orphan', backref='file')

class FileVersion(db.Model):
    __tablename__ = 'file_version'
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('file.id', ondelete='CASCADE'))
    version = db.Column(db.Integer)
    google_drive_id = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    def __repr__(self):
        return f"<FileVersion {self.id}>"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload_file', methods=['GET'])
def upload_file_form():
    return render_template('upload_file.html')

@app.route('/upload_files', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file:
        try:
            file_content = file.read()
            file_hash = hashlib.sha256(file_content).hexdigest()
            file.seek(0)

            existing_file = File.query.filter_by(hash=file_hash).first()
            if existing_file:
                latest_version = FileVersion.query.filter_by(file_id=existing_file.id).order_by(
                    FileVersion.version.desc()).first()
                new_version_number = latest_version.version + 1 if latest_version else 1

                new_version = FileVersion(file_id=existing_file.id, version=new_version_number,
                                          google_drive_id=existing_file.google_drive_id)
                db.session.add(new_version)
                db.session.commit()

                return jsonify({'id': existing_file.id, 'name': existing_file.name, 'google_drive_id': existing_file.google_drive_id}), 201

            temp_file_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    temp_file.write(file_content)
                    temp_file_path = temp_file.name

                file_metadata = {'name': file.filename}
                media = MediaFileUpload(temp_file_path, resumable=True)
                uploaded_file = drive_service.files().create(body=file_metadata, media_body=media,
                                                             fields='id').execute()

                new_file = File(name=file.filename, google_drive_id=uploaded_file.get('id'), hash=file_hash)
                db.session.add(new_file)
                db.session.commit()

                new_version = FileVersion(file_id=new_file.id, version=1, google_drive_id=uploaded_file.get('id'))
                db.session.add(new_version)
                db.session.commit()

            finally:
                if temp_file_path:
                    os.remove(temp_file_path)

            return jsonify({'id': new_file.id, 'name': new_file.name, 'google_drive_id': new_file.google_drive_id}), 201
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'No file provided'}), 400
    return redirect(url_for('list_files'))


@app.route('/files', methods=['GET'])
def list_files():
    files = File.query.all()
    return render_template('list_file.html', files=files)

@app.route('/files/details/<int:file_id>', methods=['GET'])
def get_file(file_id):
    file = File.query.get_or_404(file_id)
    file_details = {
        'file': file,  # Передаем переменную file в контекст шаблона
        'versions': [{'version': v.version, 'google_drive_id': v.google_drive_id} for v in file.versions]
    }
    return render_template('file_details.html', **file_details)

@app.route('/delete_files/<int:file_id>', methods=['DELETE'])
def delete_file(file_id):
    file = File.query.get_or_404(file_id)

    versions = FileVersion.query.filter_by(file_id=file_id).all()
    for version in versions:
        try:
            drive_service.files().get(fileId=version.google_drive_id).execute()
            drive_service.files().delete(fileId=version.google_drive_id).execute()
            logging.info(f"Deleted file version {version.version} with ID {version.google_drive_id} from Google Drive.")
        except HttpError as e:
            if e.resp.status == 404:
                logging.warning(f"File not found: {version.google_drive_id}, skipping deletion.")
            else:
                raise e

        db.session.delete(version)

    db.session.delete(file)
    db.session.commit()

    return jsonify({'message': 'File and all its versions deleted successfully'})

@app.route('/download_file/<int:file_id>', methods=['GET'])
def download_file(file_id):
    file = File.query.get_or_404(file_id)

    version = request.args.get('version')
    if version is not None:
        file_version = FileVersion.query.filter_by(file_id=file_id, version=version).first()
        if file_version:
            file_google_drive_id = file_version.google_drive_id
        else:
            return jsonify({'error': 'Specified version not found'}), 404
    else:
        file_version = FileVersion.query.filter_by(file_id=file_id).order_by(FileVersion.version.desc()).first()
        if file_version:
            file_google_drive_id = file_version.google_drive_id
        else:
            return jsonify({'error': 'No versions found for the file'}), 404

    file_content = drive_service.files().get_media(fileId=file_google_drive_id).execute()

    temp_dir = tempfile.gettempdir()
    temp_file_path = os.path.join(temp_dir, file.name)
    with io.open(temp_file_path, 'wb') as temp_file:
        temp_file.write(file_content)

    return send_file(temp_file_path, as_attachment=True)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
