#!/usr/bin/env python3
"""
Servidor 100% real para conversión de videos con FFmpeg
Versión SIN limpieza automática de sesiones
"""

import os
import sys
import uuid
import json
import time
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import mimetypes
import re

from flask import Flask, request, jsonify, send_file, send_from_directory, Response
from flask_cors import CORS

# =============== CONFIGURACIÓN ===============
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['UPLOAD_FOLDER'] = 'temp_videos'
app.config['CONVERTED_FOLDER'] = 'converted_videos'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-123')
app.config['MAX_RETENTION_MINUTES'] = 120  # 2 horas (NO se usa para sesiones)

# Crear carpetas necesarias
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['CONVERTED_FOLDER'], exist_ok=True)

# Base de datos en memoria - NO SE LIMPIA AUTOMÁTICAMENTE
sessions: Dict[str, Dict] = {}

# Configuración FFmpeg
FFMPEG_PATH = 'ffmpeg'
FFPROBE_PATH = 'ffprobe'

# Verificar que FFmpeg esté instalado
try:
    subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, check=True)
    subprocess.run([FFPROBE_PATH, '-version'], capture_output=True, check=True)
    print("✅ FFmpeg y FFprobe están instalados correctamente")
except (subprocess.CalledProcessError, FileNotFoundError):
    print("❌ Error: FFmpeg no está instalado correctamente")
    sys.exit(1)

# Configuración de calidades
VIDEO_QUALITIES = {
    '1080p': {'width': 1920, 'height': 1080, 'bitrate': '5000k', 'size_estimate': '150-250 MB'},
    '720p': {'width': 1280, 'height': 720, 'bitrate': '2500k', 'size_estimate': '80-120 MB'},
    '480p': {'width': 854, 'height': 480, 'bitrate': '1200k', 'size_estimate': '40-60 MB'},
    '360p': {'width': 640, 'height': 360, 'bitrate': '800k', 'size_estimate': '20-30 MB'},
    '240p': {'width': 426, 'height': 240, 'bitrate': '400k', 'size_estimate': '10-15 MB'}
}

# Extensiones de video permitidas
ALLOWED_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg'}

# =============== FUNCIONES AUXILIARES ===============
def generate_session_id() -> str:
    """Generar un ID de sesión único"""
    return str(uuid.uuid4())

def log_message(session_id: str, message: str, level: str = "INFO"):
    """Log con timestamp y sesión"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] [{session_id[:8]}] [{level}] {message}")
    
    if session_id in sessions:
        if 'logs' not in sessions[session_id]:
            sessions[session_id]['logs'] = []
        sessions[session_id]['logs'].append(f"[{timestamp}] {message}")

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
            'format': info['format']['format_name'],
            'bit_rate': int(info['format'].get('bit_rate', 0))
        }
        
        # Buscar stream de video
        for stream in info['streams']:
            if stream['codec_type'] == 'video':
                video_info.update({
                    'width': stream.get('width', 0),
                    'height': stream.get('height', 0),
                    'codec': stream.get('codec_name', 'unknown'),
                    'frame_rate': stream.get('r_frame_rate', '0/1')
                })
                break
        
        # Formatear duración
        minutes = int(video_info['duration'] // 60)
        seconds = int(video_info['duration'] % 60)
        video_info['duration_formatted'] = f"{minutes}:{seconds:02d}"
        
        # Formatear tamaño
        size_mb = video_info['size'] / (1024 * 1024)
        video_info['size_formatted'] = f"{size_mb:.1f} MB"
        
        # Determinar calidad aproximada
        height = video_info.get('height', 0)
        if height >= 1080:
            video_info['quality'] = '1080p'
        elif height >= 720:
            video_info['quality'] = '720p'
        elif height >= 480:
            video_info['quality'] = '480p'
        elif height >= 360:
            video_info['quality'] = '360p'
        else:
            video_info['quality'] = '240p'
        
        return video_info
        
    except Exception as e:
        return {
            'duration': 0,
            'size': 0,
            'duration_formatted': '0:00',
            'size_formatted': '0 MB',
            'width': 0,
            'height': 0,
            'quality': 'Desconocida',
            'error': str(e)
        }

def download_video_direct(url: str, output_path: str, session_id: str) -> Tuple[bool, str, Dict]:
    """Descargar video usando wget"""
    try:
        # Comando wget simple
        cmd = ['wget', '-O', output_path, '--timeout=60', '--tries=2', url]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
        
        # Verificar que el archivo existe
        if not os.path.exists(output_path):
            return False, "Archivo no se descargó correctamente", {}
        
        file_size = os.path.getsize(output_path)
        if file_size == 0:
            os.remove(output_path)
            return False, "Archivo descargado está vacío", {}
        
        # Obtener información del video
        video_info = get_video_info(output_path)
        video_info['title'] = Path(url).name.split('?')[0]
        
        return True, output_path, video_info
        
    except subprocess.TimeoutExpired:
        return False, "Tiempo de espera agotado en la descarga", {}
    except Exception as e:
        return False, f"Error en descarga: {str(e)}", {}

def convert_video_ffmpeg(input_path: str, output_path: str, quality: str, session_id: str) -> Tuple[bool, str]:
    """Convertir video usando FFmpeg"""
    try:
        if quality not in VIDEO_QUALITIES:
            return False, f"Calidad no válida: {quality}"
        
        config = VIDEO_QUALITIES[quality]
        
        # Comando FFmpeg simple
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
        
        # Ejecutar FFmpeg
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        
        if result.returncode != 0:
            return False, f"FFmpeg error: {result.stderr[:200]}"
        
        return True, output_path
        
    except subprocess.TimeoutExpired:
        return False, "Tiempo de espera agotado en la conversión"
    except Exception as e:
        return False, f"Error en conversión: {str(e)}"

# =============== RUTAS API ===============
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
                "message": "URL debe comenzar con http:// o https://"
            }), 400
        
        # Crear sesión
        session_id = generate_session_id()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_filename = f"original_{session_id}_{timestamp}.mp4"
        original_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        
        # Crear sesión (NO se eliminará automáticamente)
        sessions[session_id] = {
            "url": url,
            "status": "downloading",
            "original_path": original_path,
            "created_at": datetime.now().isoformat(),
            "conversion_progress": 0,
            "download_progress": 0,
            "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] Sesión creada"]
        }
        
        log_message(session_id, f"Iniciando descarga: {url[:50]}...")
        
        def download_background():
            """Función de descarga en segundo plano"""
            try:
                sessions[session_id]['download_progress'] = 10
                
                # Descargar video
                success, result, video_info = download_video_direct(url, original_path, session_id)
                
                if success:
                    sessions[session_id].update({
                        "status": "downloaded",
                        "video_info": video_info,
                        "downloaded_at": datetime.now().isoformat(),
                        "original_size": os.path.getsize(original_path),
                        "download_progress": 100
                    })
                    
                    log_message(session_id, 
                        f"✅ Descarga completada: {video_info.get('width', 0)}x{video_info.get('height', 0)}, "
                        f"{video_info.get('duration_formatted', '0:00')}"
                    )
                    
                else:
                    sessions[session_id].update({
                        "status": "error",
                        "error": result
                    })
                    log_message(session_id, f"❌ Error en descarga: {result}", "ERROR")
                    
            except Exception as e:
                sessions[session_id].update({
                    "status": "error",
                    "error": str(e)
                })
                log_message(session_id, f"💥 Error inesperado: {str(e)}", "ERROR")
        
        # Iniciar descarga en segundo plano
        download_thread = threading.Thread(target=download_background, daemon=True)
        download_thread.start()
        
        # Preparar calidades disponibles
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
            "message": "Iniciando descarga...",
            "logs": sessions[session_id]['logs']
        })
        
    except Exception as e:
        print(f"❌ Error en process_video: {str(e)}")
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
        
        print(f"🔍 Buscando sesión: {session_id}")
        print(f"📊 Sesiones existentes: {list(sessions.keys())}")
        
        if session_id not in sessions:
            print(f"❌ Sesión NO encontrada: {session_id}")
            return jsonify({
                "success": False,
                "message": f"Sesión no encontrada. ID: {session_id}"
            }), 404
        
        session_data = sessions[session_id]
        print(f"📋 Estado de sesión: {session_data.get('status')}")
        
        if session_data.get("status") != "downloaded":
            return jsonify({
                "success": False,
                "message": f"Video no descargado. Estado: {session_data.get('status')}"
            }), 400
        
        if quality not in VIDEO_QUALITIES:
            return jsonify({
                "success": False,
                "message": f"Calidad no válida"
            }), 400
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_filename = f"converted_{session_id}_{quality}_{timestamp}.mp4"
        output_path = os.path.join(app.config['CONVERTED_FOLDER'], output_filename)
        
        # Actualizar estado inmediatamente
        sessions[session_id].update({
            "status": "converting",
            "target_quality": quality,
            "output_path": output_path,
            "conversion_started": datetime.now().isoformat(),
            "conversion_progress": 0
        })
        
        log_message(session_id, f"Iniciando conversión a {quality}")
        
        def convert_background():
            """Función de conversión en segundo plano"""
            try:
                # Simular progreso inicial
                for i in range(1, 11):
                    if session_id in sessions:
                        sessions[session_id]['conversion_progress'] = i * 10
                    time.sleep(0.5)
                
                # Convertir video REAL
                success, result = convert_video_ffmpeg(
                    session_data['original_path'],
                    output_path,
                    quality,
                    session_id
                )
                
                if success:
                    # Obtener info del video convertido
                    converted_info = get_video_info(output_path)
                    
                    sessions[session_id].update({
                        "status": "completed",
                        "converted_at": datetime.now().isoformat(),
                        "conversion_progress": 100,
                        "converted_path": output_path,
                        "converted_info": converted_info
                    })
                    
                    log_message(session_id, f"🎉 Conversión completada: {quality}")
                    
                else:
                    sessions[session_id].update({
                        "status": "conversion_error",
                        "error": result
                    })
                    log_message(session_id, f"❌ Error en conversión: {result}", "ERROR")
                    
            except Exception as e:
                sessions[session_id].update({
                    "status": "conversion_error",
                    "error": str(e)
                })
                log_message(session_id, f"💥 Error inesperado: {str(e)}", "ERROR")
        
        # Iniciar conversión en segundo plano
        convert_thread = threading.Thread(target=convert_background, daemon=True)
        convert_thread.start()
        
        return jsonify({
            "success": True,
            "message": f"Conversión a {quality} iniciada",
            "session_id": session_id,
            "status": "converting"
        })
        
    except Exception as e:
        print(f"❌ Error en convert_video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/status/<session_id>')
def get_status(session_id):
    """Obtener estado de la conversión"""
    try:
        print(f"📡 Consultando estado para: {session_id}")
        print(f"📊 Todas las sesiones: {list(sessions.keys())}")
        
        if session_id not in sessions:
            print(f"❌ Sesión {session_id} NO existe en sessions dict")
            return jsonify({
                "success": False,
                "message": f"Sesión no encontrada: {session_id}"
            }), 404
        
        session_data = sessions[session_id]
        
        response = {
            "success": True,
            "session_id": session_id,
            "status": session_data.get("status", "unknown"),
            "conversion_progress": session_data.get("conversion_progress", 0),
            "download_progress": session_data.get("download_progress", 0),
            "error": session_data.get("error"),
            "logs": session_data.get("logs", []),
            "timestamp": datetime.now().isoformat()
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
        
        print(f"✅ Estado devuelto: {session_data.get('status')}")
        return jsonify(response)
        
    except Exception as e:
        print(f"❌ Error en get_status: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/stream/<session_id>')
def stream_video(session_id):
    """Transmitir video convertido"""
    try:
        if session_id not in sessions:
            return jsonify({"success": False, "message": "Sesión no encontrada"}), 404
        
        session_data = sessions[session_id]
        
        if "converted_path" not in session_data or not os.path.exists(session_data["converted_path"]):
            return jsonify({"success": False, "message": "Video no encontrado"}), 404
        
        video_path = session_data["converted_path"]
        
        return send_file(
            video_path,
            mimetype='video/mp4',
            as_attachment=False,
            download_name=f"video_{session_data.get('target_quality', 'converted')}.mp4"
        )
        
    except Exception as e:
        print(f"❌ Error en stream_video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/download/<session_id>')
def download_video(session_id):
    """Descargar video convertido"""
    try:
        if session_id not in sessions:
            return jsonify({"success": False, "message": "Sesión no encontrada"}), 404
        
        session_data = sessions[session_id]
        
        if "converted_path" not in session_data or not os.path.exists(session_data["converted_path"]):
            return jsonify({"success": False, "message": "Video no encontrado"}), 404
        
        video_path = session_data["converted_path"]
        quality = session_data.get("target_quality", "converted")
        
        return send_file(
            video_path,
            mimetype='video/mp4',
            as_attachment=True,
            download_name=f"video_{quality}_{session_id}.mp4"
        )
        
    except Exception as e:
        print(f"❌ Error en download_video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/cleanup', methods=['POST'])
def manual_cleanup():
    """Limpieza manual de archivos viejos (opcional)"""
    try:
        cleaned = 0
        
        folders = [app.config['UPLOAD_FOLDER'], app.config['CONVERTED_FOLDER']]
        for folder in folders:
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                if os.path.isfile(filepath):
                    try:
                        # Eliminar archivos con más de 1 hora
                        if time.time() - os.path.getmtime(filepath) > 3600:
                            os.remove(filepath)
                            cleaned += 1
                    except:
                        pass
        
        return jsonify({
            "success": True,
            "cleaned": cleaned,
            "message": f"Se limpiaron {cleaned} archivos"
        })
        
    except Exception as e:
        print(f"❌ Error en cleanup: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/reset_session/<session_id>', methods=['POST'])
def reset_session(session_id):
    """Reiniciar una sesión específica"""
    try:
        if session_id in sessions:
            sessions[session_id]['status'] = 'ready'
            sessions[session_id]['conversion_progress'] = 0
            return jsonify({
                "success": True,
                "message": f"Sesión {session_id} reiniciada"
            })
        else:
            return jsonify({
                "success": False,
                "message": "Sesión no encontrada"
            }), 404
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

@app.route('/api/list_sessions')
def list_sessions():
    """Listar todas las sesiones activas (para debug)"""
    sessions_list = []
    for session_id, data in sessions.items():
        sessions_list.append({
            "id": session_id,
            "status": data.get("status", "unknown"),
            "created_at": data.get("created_at", ""),
            "conversion_progress": data.get("conversion_progress", 0)
        })
    
    return jsonify({
        "success": True,
        "sessions": sessions_list,
        "total": len(sessions)
    })

@app.route('/api/health')
def health_check():
    """Endpoint de salud"""
    try:
        subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, check=True)
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "ffmpeg": "available",
            "sessions_active": len(sessions),
            "disk_usage": {
                "temp_videos": f"{sum(os.path.getsize(f) for f in Path(app.config['UPLOAD_FOLDER']).glob('*') if f.is_file()) / (1024*1024):.1f} MB",
                "converted_videos": f"{sum(os.path.getsize(f) for f in Path(app.config['CONVERTED_FOLDER']).glob('*') if f.is_file()) / (1024*1024):.1f} MB"
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
    print("🚀 CONVERTIDOR DE VIDEOS FFMPEG - SIN LIMPIEZA AUTOMÁTICA")
    print("=" * 60)
    print(f"📡 URL: http://0.0.0.0:{port}")
    print(f"🔧 FFmpeg: {FFMPEG_PATH}")
    print(f"💾 Sesiones: NUNCA se eliminan automáticamente")
    print(f"📁 Temp: {app.config['UPLOAD_FOLDER']}")
    print(f"📁 Convertidos: {app.config['CONVERTED_FOLDER']}")
    print("=" * 60)
    print("✅ Sistema listo - Las sesiones PERSISTEN")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)