#!/usr/bin/env python3
"""
Servidor de conversión de videos SIN SESIONES
Proceso directo: URL → Descarga → Conversión → Resultado
"""

import os
import sys
import json
import time
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import mimetypes
import hashlib

from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

# =============== CONFIGURACIÓN ===============
app = Flask(__name__)
CORS(app)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['UPLOAD_FOLDER'] = 'temp_videos'
app.config['CONVERTED_FOLDER'] = 'converted_videos'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-123')

# Crear carpetas necesarias
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['CONVERTED_FOLDER'], exist_ok=True)

# Configuración FFmpeg
FFMPEG_PATH = 'ffmpeg'
FFPROBE_PATH = 'ffprobe'

# Verificar que FFmpeg esté instalado
try:
    subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, check=True)
    print("✅ FFmpeg está instalado correctamente")
except (subprocess.CalledProcessError, FileNotFoundError):
    print("❌ Error: FFmpeg no está instalado")
    sys.exit(1)

# Configuración de calidades
VIDEO_QUALITIES = {
    '1080p': {'width': 1920, 'height': 1080, 'bitrate': '5000k', 'size': '150-250 MB'},
    '720p': {'width': 1280, 'height': 720, 'bitrate': '2500k', 'size': '80-120 MB'},
    '480p': {'width': 854, 'height': 480, 'bitrate': '1200k', 'size': '40-60 MB'},
    '360p': {'width': 640, 'height': 360, 'bitrate': '800k', 'size': '20-30 MB'},
    '240p': {'width': 426, 'height': 240, 'bitrate': '400k', 'size': '10-15 MB'}
}

# =============== FUNCIONES AUXILIARES ===============
def generate_file_id(url: str, quality: str) -> str:
    """Generar un ID único basado en URL y calidad"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{url_hash}_{quality}_{timestamp}"

def get_video_info(video_path: str) -> Dict:
    """Obtener información del video"""
    try:
        cmd = [
            FFPROBE_PATH,
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            video_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        
        video_info = {
            'duration': float(info['format']['duration']),
            'size': int(info['format']['size']),
            'format': info['format']['format_name']
        }
        
        for stream in info['streams']:
            if stream['codec_type'] == 'video':
                video_info.update({
                    'width': stream.get('width', 0),
                    'height': stream.get('height', 0),
                    'codec': stream.get('codec_name', 'unknown')
                })
                break
        
        # Formatear
        minutes = int(video_info['duration'] // 60)
        seconds = int(video_info['duration'] % 60)
        video_info['duration_formatted'] = f"{minutes}:{seconds:02d}"
        
        size_mb = video_info['size'] / (1024 * 1024)
        video_info['size_formatted'] = f"{size_mb:.1f} MB"
        
        return video_info
        
    except Exception as e:
        return {
            'duration': 0,
            'size': 0,
            'duration_formatted': '0:00',
            'size_formatted': '0 MB',
            'width': 0,
            'height': 0
        }

def download_video(url: str, output_path: str) -> bool:
    """Descargar video usando wget"""
    try:
        cmd = ['wget', '-O', output_path, '--timeout=30', '--tries=2', url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except:
        return False

def convert_video(input_path: str, output_path: str, quality: str) -> bool:
    """Convertir video con FFmpeg"""
    try:
        if quality not in VIDEO_QUALITIES:
            return False
        
        config = VIDEO_QUALITIES[quality]
        
        cmd = [
            FFMPEG_PATH,
            '-i', input_path,
            '-vf', f'scale={config["width"]}:{config["height"]}',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-y',
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        return result.returncode == 0
    except:
        return False

# =============== RUTAS API ===============
@app.route('/')
def index():
    """Servir la página principal"""
    return send_file('index.html')

@app.route('/api/qualities')
def get_qualities():
    """Obtener lista de calidades disponibles"""
    qualities = []
    for name, config in VIDEO_QUALITIES.items():
        qualities.append({
            'quality': name,
            'resolution': f"{config['width']}x{config['height']}",
            'bitrate': config['bitrate'],
            'size': config['size']
        })
    
    return jsonify({
        'success': True,
        'qualities': qualities
    })

@app.route('/api/process', methods=['POST'])
def process_video():
    """Procesar video COMPLETO: descargar y convertir"""
    try:
        data = request.get_json()
        if not data or 'url' not in data or 'quality' not in data:
            return jsonify({
                "success": False,
                "message": "URL y calidad requeridas"
            }), 400
        
        url = data['url'].strip()
        quality = data['quality']
        
        if not url.startswith(('http://', 'https://')):
            return jsonify({
                "success": False,
                "message": "URL inválida"
            }), 400
        
        if quality not in VIDEO_QUALITIES:
            return jsonify({
                "success": False,
                "message": f"Calidad no válida. Opciones: {', '.join(VIDEO_QUALITIES.keys())}"
            }), 400
        
        print(f"🎬 Procesando: {url[:50]}... a {quality}")
        
        # Generar nombres de archivo
        file_id = generate_file_id(url, quality)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"temp_{file_id}.mp4")
        output_path = os.path.join(app.config['CONVERTED_FOLDER'], f"converted_{file_id}.mp4")
        
        # 1. Descargar video
        print(f"⬇️  Descargando video...")
        if not download_video(url, temp_path):
            return jsonify({
                "success": False,
                "message": "Error al descargar el video"
            }), 500
        
        # 2. Obtener información del original
        original_info = get_video_info(temp_path)
        
        # 3. Convertir video
        print(f"🔄 Convirtiendo a {quality}...")
        if not convert_video(temp_path, output_path, quality):
            # Limpiar archivo temporal
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return jsonify({
                "success": False,
                "message": "Error al convertir el video"
            }), 500
        
        # 4. Obtener información del convertido
        converted_info = get_video_info(output_path)
        
        # 5. Limpiar temporal
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        print(f"✅ Conversión completada: {output_path}")
        
        return jsonify({
            "success": True,
            "message": "Video convertido exitosamente",
            "original_info": original_info,
            "converted_info": converted_info,
            "quality": quality,
            "download_url": f"/api/download/{file_id}",
            "stream_url": f"/api/stream/{file_id}",
            "filename": f"video_{quality}_{file_id}.mp4"
        })
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/stream/<file_id>')
def stream_video(file_id):
    """Transmitir video convertido"""
    try:
        # Buscar archivo
        converted_files = list(Path(app.config['CONVERTED_FOLDER']).glob(f"converted_{file_id}.mp4"))
        
        if not converted_files:
            return jsonify({"success": False, "message": "Video no encontrado"}), 404
        
        video_path = str(converted_files[0])
        
        return send_file(
            video_path,
            mimetype='video/mp4',
            as_attachment=False
        )
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

@app.route('/api/download/<file_id>')
def download_video(file_id):
    """Descargar video convertido"""
    try:
        # Buscar archivo
        converted_files = list(Path(app.config['CONVERTED_FOLDER']).glob(f"converted_{file_id}.mp4"))
        
        if not converted_files:
            return jsonify({"success": False, "message": "Video no encontrado"}), 404
        
        video_path = str(converted_files[0])
        
        # Extraer calidad del nombre
        parts = file_id.split('_')
        quality = parts[1] if len(parts) > 1 else 'converted'
        
        return send_file(
            video_path,
            mimetype='video/mp4',
            as_attachment=True,
            download_name=f"video_{quality}.mp4"
        )
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

@app.route('/api/cleanup', methods=['POST'])
def cleanup():
    """Limpiar archivos antiguos"""
    try:
        cleaned = 0
        now = time.time()
        
        # Limpiar archivos con más de 1 hora
        for folder in [app.config['UPLOAD_FOLDER'], app.config['CONVERTED_FOLDER']:
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                if os.path.isfile(filepath):
                    if now - os.path.getmtime(filepath) > 3600:  # 1 hora
                        os.remove(filepath)
                        cleaned += 1
        
        return jsonify({
            "success": True,
            "cleaned": cleaned,
            "message": f"Se limpiaron {cleaned} archivos"
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

@app.route('/api/health')
def health_check():
    """Endpoint de salud"""
    try:
        subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, check=True)
        
        # Contar archivos
        temp_count = len(list(Path(app.config['UPLOAD_FOLDER']).glob('*')))
        converted_count = len(list(Path(app.config['CONVERTED_FOLDER']).glob('*')))
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "ffmpeg": "available",
            "files": {
                "temp": temp_count,
                "converted": converted_count
            }
        })
        
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"success": False, "message": "Endpoint no encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"success": False, "message": "Error interno del servidor"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("🚀 CONVERTIDOR DE VIDEOS FFMPEG - SIN SESIONES")
    print("=" * 60)
    print(f"📡 URL: http://0.0.0.0:{port}")
    print(f"🔧 FFmpeg: {FFMPEG_PATH}")
    print(f"🎯 Calidades: {', '.join(VIDEO_QUALITIES.keys())}")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)