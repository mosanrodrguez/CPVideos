#!/usr/bin/env python3
"""
Servidor real para conversión de videos con FFmpeg
Versión optimizada para Render.com
"""

import os
import sys
import uuid
import json
import time
import signal
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import mimetypes

from flask import Flask, request, jsonify, send_file, send_from_directory, Response
from flask_cors import CORS
import yt_dlp

# Configuración de la aplicación
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Configuración
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['UPLOAD_FOLDER'] = 'temp_videos'
app.config['CONVERTED_FOLDER'] = 'converted_videos'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-123')
app.config['MAX_RETENTION_MINUTES'] = int(os.environ.get('MAX_RETENTION_MINUTES', '60'))

# Crear carpetas necesarias
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['CONVERTED_FOLDER'], exist_ok=True)

# Base de datos en memoria
sessions: Dict[str, Dict] = {}
conversion_tasks: Dict[str, Dict] = {}

# Configuración FFmpeg
FFMPEG_PATH = 'ffmpeg'
FFPROBE_PATH = 'ffprobe'

# Verificar que FFmpeg esté instalado
try:
    subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, check=True)
    subprocess.run([FFPROBE_PATH, '-version'], capture_output=True, check=True)
    print("✓ FFmpeg y FFprobe están instalados correctamente")
except (subprocess.CalledProcessError, FileNotFoundError):
    print("✗ Error: FFmpeg no está instalado correctamente")
    sys.exit(1)

# Configuración de calidades
VIDEO_QUALITIES = {
    '1080p': {'width': 1920, 'height': 1080, 'bitrate': '8000k', 'size_estimate': '200 MB'},
    '720p': {'width': 1280, 'height': 720, 'bitrate': '4000k', 'size_estimate': '100 MB'},
    '480p': {'width': 854, 'height': 480, 'bitrate': '2000k', 'size_estimate': '50 MB'},
    '360p': {'width': 640, 'height': 360, 'bitrate': '1000k', 'size_estimate': '25 MB'},
    '240p': {'width': 426, 'height': 240, 'bitrate': '500k', 'size_estimate': '12 MB'}
}

def generate_session_id() -> str:
    """Generar un ID de sesión único"""
    return str(uuid.uuid4())

def get_video_info(video_path: str) -> Dict:
    """Obtener información del video usando FFprobe"""
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
        
        minutes = int(video_info['duration'] // 60)
        seconds = int(video_info['duration'] % 60)
        video_info['duration_formatted'] = f"{minutes}:{seconds:02d}"
        
        size_mb = video_info['size'] / (1024 * 1024)
        video_info['size_formatted'] = f"{size_mb:.1f} MB"
        
        return video_info
        
    except Exception as e:
        app.logger.error(f"Error obteniendo info del video: {str(e)}")
        return {
            'duration': 0,
            'size': 0,
            'duration_formatted': '0:00',
            'size_formatted': '0 MB',
            'width': 0,
            'height': 0
        }

def download_youtube_video(url: str, output_path: str) -> Tuple[bool, str, Dict]:
    """Descargar video de YouTube usando yt-dlp"""
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            ydl.download([url])
            
            video_info = {
                'title': info.get('title', 'Video descargado'),
                'duration': info.get('duration', 0),
                'original_quality': f"{info.get('height', 0)}p",
                'channel': info.get('uploader', 'Desconocido')
            }
            
            return True, output_path, video_info
            
    except Exception as e:
        app.logger.error(f"Error descargando video de YouTube: {str(e)}")
        return False, str(e), {}

def convert_video_with_ffmpeg(
    input_path: str, 
    output_path: str, 
    quality: str,
    session_id: str
) -> Tuple[bool, str]:
    """Convertir video a calidad específica usando FFmpeg"""
    try:
        if quality not in VIDEO_QUALITIES:
            return False, f"Calidad no válida: {quality}"
        
        config = VIDEO_QUALITIES[quality]
        
        cmd = [
            FFMPEG_PATH,
            '-i', input_path,
            '-vf', f'scale={config["width"]}:{config["height"]}',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-b:v', config['bitrate'],
            '-c:a', 'aac',
            '-b:a', '128k',
            '-y',
            output_path
        ]
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        conversion_tasks[session_id] = {
            'process': process,
            'output_path': output_path,
            'start_time': datetime.now().isoformat()
        }
        
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            return True, output_path
        else:
            return False, f"Error FFmpeg: {stderr[:200]}"
            
    except Exception as e:
        app.logger.error(f"Error convirtiendo video: {str(e)}")
        return False, str(e)

def cleanup_old_files():
    """Limpiar archivos antiguos automáticamente - CORREGIDO"""
    try:
        now = time.time()
        cutoff = now - (app.config['MAX_RETENTION_MINUTES'] * 60)
        
        # Limpiar archivos temporales - CORREGIDO: falta cerrar corchete
        folders = [app.config['UPLOAD_FOLDER'], app.config['CONVERTED_FOLDER']]
        for folder in folders:
            if not os.path.exists(folder):
                continue
                
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                if os.path.isfile(filepath):
                    try:
                        if os.path.getmtime(filepath) < cutoff:
                            os.remove(filepath)
                            app.logger.info(f"Eliminado archivo antiguo: {filepath}")
                    except Exception as e:
                        app.logger.error(f"Error eliminando archivo: {str(e)}")
        
        # Limpiar sesiones antiguas
        sessions_to_remove = []
        for session_id, session_data in sessions.items():
            created_at = datetime.fromisoformat(session_data["created_at"]).timestamp()
            if created_at < cutoff:
                sessions_to_remove.append(session_id)
        
        for session_id in sessions_to_remove:
            if session_id in conversion_tasks:
                task = conversion_tasks[session_id]
                if 'process' in task:
                    task['process'].terminate()
                conversion_tasks.pop(session_id, None)
            sessions.pop(session_id, None)
            
    except Exception as e:
        app.logger.error(f"Error en limpieza automática: {str(e)}")

# Iniciar limpieza automática en segundo plano
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    """Servir la página principal"""
    return send_file('index.html')

@app.route('/api/process', methods=['POST'])
def process_video():
    """Procesar URL de video"""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                "success": False,
                "message": "URL no proporcionada"
            }), 400
        
        url = data['url'].strip()
        
        if not url.startswith(('http://', 'https://')):
            return jsonify({
                "success": False,
                "message": "URL no válida"
            }), 400
        
        session_id = generate_session_id()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        is_youtube = 'youtube.com' in url or 'youtu.be' in url
        original_filename = f"original_{session_id}_{timestamp}.mp4"
        original_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        
        sessions[session_id] = {
            "url": url,
            "status": "downloading",
            "original_path": original_path,
            "created_at": datetime.now().isoformat(),
            "conversion_progress": 0,
            "is_youtube": is_youtube
        }
        
        def download_background():
            try:
                if is_youtube:
                    success, result, video_info = download_youtube_video(url, original_path)
                else:
                    # Para URLs directas, usar wget
                    try:
                        cmd = ['wget', '-O', original_path, url]
                        subprocess.run(cmd, capture_output=True, text=True, check=True)
                        success = True
                        result = original_path
                        video_info = get_video_info(original_path)
                        video_info['title'] = Path(original_path).name
                    except Exception as e:
                        success = False
                        result = str(e)
                        video_info = {}
                
                if success:
                    ffprobe_info = get_video_info(original_path)
                    video_info.update(ffprobe_info)
                    
                    sessions[session_id].update({
                        "status": "downloaded",
                        "video_info": video_info,
                        "downloaded_at": datetime.now().isoformat(),
                        "original_size": os.path.getsize(original_path)
                    })
                else:
                    sessions[session_id].update({
                        "status": "error",
                        "error": result
                    })
                    
            except Exception as e:
                sessions[session_id].update({
                    "status": "error",
                    "error": str(e)
                })
        
        thread = threading.Thread(target=download_background, daemon=True)
        thread.start()
        
        available_qualities = []
        for quality, config in VIDEO_QUALITIES.items():
            available_qualities.append({
                "quality": quality,
                "resolution": f"{config['width']}x{config['height']}",
                "bitrate": config['bitrate'],
                "size": config['size_estimate']
            })
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "qualities": available_qualities,
            "message": "Video en proceso de descarga"
        })
        
    except Exception as e:
        app.logger.error(f"Error procesando video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/convert', methods=['POST'])
def convert_video():
    """Convertir video a calidad específica"""
    try:
        data = request.get_json()
        if not data or 'session_id' not in data or 'quality' not in data:
            return jsonify({
                "success": False,
                "message": "Datos incompletos"
            }), 400
        
        session_id = data['session_id']
        quality = data['quality']
        
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": "Sesión no encontrada"
            }), 404
        
        session_data = sessions[session_id]
        
        if session_data.get("status") != "downloaded":
            return jsonify({
                "success": False,
                "message": "Video no descargado aún"
            }), 400
        
        if quality not in VIDEO_QUALITIES:
            return jsonify({
                "success": False,
                "message": f"Calidad no válida. Opciones: {', '.join(VIDEO_QUALITIES.keys())}"
            }), 400
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_filename = f"converted_{session_id}_{quality}_{timestamp}.mp4"
        output_path = os.path.join(app.config['CONVERTED_FOLDER'], output_filename)
        
        sessions[session_id].update({
            "status": "converting",
            "target_quality": quality,
            "output_path": output_path,
            "conversion_started": datetime.now().isoformat(),
            "conversion_progress": 0
        })
        
        def convert_background():
            try:
                success, result = convert_video_with_ffmpeg(
                    session_data['original_path'],
                    output_path,
                    quality,
                    session_id
                )
                
                if success:
                    sessions[session_id].update({
                        "status": "completed",
                        "converted_at": datetime.now().isoformat(),
                        "conversion_progress": 100,
                        "converted_path": output_path
                    })
                    
                    converted_info = get_video_info(output_path)
                    sessions[session_id]['converted_info'] = converted_info
                    
                else:
                    sessions[session_id].update({
                        "status": "error",
                        "error": result,
                        "conversion_progress": 0
                    })
                    
            except Exception as e:
                sessions[session_id].update({
                    "status": "error",
                    "error": str(e)
                })
        
        thread = threading.Thread(target=convert_background, daemon=True)
        thread.start()
        
        return jsonify({
            "success": True,
            "message": f"Conversión a {quality} iniciada",
            "session_id": session_id
        })
        
    except Exception as e:
        app.logger.error(f"Error convirtiendo video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/status/<session_id>')
def get_status(session_id):
    """Obtener estado de la conversión"""
    try:
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": "Sesión no encontrada"
            }), 404
        
        session_data = sessions[session_id]
        
        response = {
            "success": True,
            "session_id": session_id,
            "status": session_data.get("status", "unknown"),
            "conversion_progress": session_data.get("conversion_progress", 0),
            "error": session_data.get("error")
        }
        
        if "video_info" in session_data:
            response["video_info"] = session_data["video_info"]
        
        if "converted_info" in session_data:
            response["converted_info"] = session_data["converted_info"]
            response["converted"] = True
            response["video_url"] = f"/api/stream/{session_id}"
            response["download_url"] = f"/api/download/{session_id}"
            response["quality"] = session_data.get("target_quality")
        else:
            response["converted"] = False
        
        return jsonify(response)
        
    except Exception as e:
        app.logger.error(f"Error obteniendo estado: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/stream/<session_id>')
def stream_video(session_id):
    """Transmitir video convertido"""
    try:
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": "Sesión no encontrada"
            }), 404
        
        session_data = sessions[session_id]
        
        if "converted_path" not in session_data or not os.path.exists(session_data["converted_path"]):
            return jsonify({
                "success": False,
                "message": "Video no encontrado"
            }), 404
        
        video_path = session_data["converted_path"]
        
        mime_type, _ = mimetypes.guess_type(video_path)
        if not mime_type:
            mime_type = 'video/mp4'
        
        file_size = os.path.getsize(video_path)
        range_header = request.headers.get('Range')
        
        if range_header:
            byte1, byte2 = 0, None
            range_ = range_header.replace('bytes=', '').split('-')
            byte1 = int(range_[0])
            if len(range_) == 2 and range_[1]:
                byte2 = int(range_[1])
            
            length = file_size - byte1
            if byte2 is not None:
                length = byte2 - byte1 + 1
            
            def generate():
                with open(video_path, 'rb') as f:
                    f.seek(byte1)
                    remaining = length
                    while remaining > 0:
                        chunk_size = min(4096, remaining)
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
            
            rv = Response(generate(), 206, mimetype=mime_type)
            rv.headers.add('Content-Range', f'bytes {byte1}-{byte1 + length - 1}/{file_size}')
            rv.headers.add('Content-Length', str(length))
            rv.headers.add('Accept-Ranges', 'bytes')
            return rv
        
        else:
            return send_file(
                video_path,
                mimetype=mime_type,
                as_attachment=False,
                download_name=f"video_{session_data.get('target_quality', 'converted')}.mp4"
            )
        
    except Exception as e:
        app.logger.error(f"Error transmitiendo video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/download/<session_id>')
def download_video(session_id):
    """Descargar video convertido"""
    try:
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": "Sesión no encontrada"
            }), 404
        
        session_data = sessions[session_id]
        
        if "converted_path" not in session_data or not os.path.exists(session_data["converted_path"]):
            return jsonify({
                "success": False,
                "message": "Video no encontrado"
            }), 404
        
        video_path = session_data["converted_path"]
        quality = session_data.get("target_quality", "converted")
        
        return send_file(
            video_path,
            mimetype='video/mp4',
            as_attachment=True,
            download_name=f"video_{quality}_{session_id}.mp4"
        )
        
    except Exception as e:
        app.logger.error(f"Error descargando video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel_conversion(session_id):
    """Cancelar conversión en progreso"""
    try:
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": "Sesión no encontrada"
            }), 404
        
        if session_id in conversion_tasks:
            task = conversion_tasks[session_id]
            if 'process' in task:
                task['process'].terminate()
                time.sleep(0.5)
                if task['process'].poll() is None:
                    task['process'].kill()
            conversion_tasks.pop(session_id, None)
        
        sessions[session_id]['status'] = 'cancelled'
        
        return jsonify({
            "success": True,
            "message": "Conversión cancelada"
        })
        
    except Exception as e:
        app.logger.error(f"Error cancelando conversión: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/cleanup', methods=['POST'])
def cleanup_files():
    """Limpiar archivos manualmente"""
    try:
        cleaned = 0
        
        folders = [app.config['UPLOAD_FOLDER'], app.config['CONVERTED_FOLDER']]
        for folder in folders:
            if not os.path.exists(folder):
                continue
                
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                if os.path.isfile(filepath):
                    try:
                        os.remove(filepath)
                        cleaned += 1
                    except:
                        pass
        
        sessions.clear()
        conversion_tasks.clear()
        
        return jsonify({
            "success": True,
            "cleaned": cleaned,
            "message": f"Se limpiaron {cleaned} archivos"
        })
        
    except Exception as e:
        app.logger.error(f"Error en limpieza: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/health')
def health_check():
    """Endpoint de salud para Render.com"""
    try:
        subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, check=True)
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "ffmpeg": "available",
            "sessions_active": len(sessions),
            "conversions_active": len(conversion_tasks)
        })
        
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('.', 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "success": False,
        "message": "Endpoint no encontrado"
    }), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        "success": False,
        "message": "Error interno del servidor"
    }), 500

@app.errorhandler(413)
def request_too_large(error):
    return jsonify({
        "success": False,
        "message": f"Archivo demasiado grande. Límite: {app.config['MAX_CONTENT_LENGTH'] / (1024*1024)} MB"
    }), 413

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Servidor iniciado en http://0.0.0.0:{port}")
    print(f"📁 Directorio de trabajo: {os.getcwd()}")
    print(f"🔧 FFmpeg disponible: {FFMPEG_PATH}")
    print(f"🎯 Calidades disponibles: {', '.join(VIDEO_QUALITIES.keys())}")
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)