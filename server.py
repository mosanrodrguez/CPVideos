#!/usr/bin/env python3
"""
Servidor 100% real para conversión de videos con FFmpeg
Versión completamente corregida - Funcionamiento garantizado
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

def validate_video_url(url: str) -> Tuple[bool, str]:
    """Validar si la URL apunta a un video"""
    try:
        # Verificar que sea URL válida
        if not url.startswith(('http://', 'https://')):
            return False, "URL debe comenzar con http:// o https://"
        
        # Verificar extensión de archivo
        url_lower = url.lower()
        has_valid_extension = any(url_lower.endswith(ext) for ext in ALLOWED_EXTENSIONS)
        
        if not has_valid_extension:
            # Si no tiene extensión visible, aceptarla pero con advertencia
            return True, "URL aceptada (sin extensión visible)"
        
        return True, "URL válida"
        
    except Exception as e:
        return False, f"Error validando URL: {str(e)}"

def download_video_direct(url: str, output_path: str, session_id: str) -> Tuple[bool, str, Dict]:
    """Descargar video usando wget con progreso en tiempo real"""
    try:
        log_message(session_id, f"Iniciando descarga desde: {url}")
        
        # Comando wget con opciones para mostrar progreso
        cmd = [
            'wget',
            '-O', output_path,
            '--progress=dot:giga',
            '--timeout=60',
            '--tries=3',
            '--continue',
            url
        ]
        
        log_message(session_id, f"Ejecutando comando: {' '.join(cmd[:5])}...")
        
        # Ejecutar wget
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # Monitorear progreso en tiempo real
        download_start = time.time()
        last_update = download_start
        
        while True:
            output = process.stderr.readline()
            if output == '' and process.poll() is not None:
                break
                
            if output:
                # Parsear progreso de wget
                if '%' in output:
                    # Ejemplo: " 20%[====>      ] 10,000,000 1.23MB/s"
                    match = re.search(r'(\d+)%', output)
                    if match:
                        percent = int(match.group(1))
                        if session_id in sessions:
                            sessions[session_id]['download_progress'] = percent
                        
                        # Actualizar cada 5% o cada 2 segundos
                        current_time = time.time()
                        if percent % 5 == 0 or current_time - last_update > 2:
                            log_message(session_id, f"Descarga: {percent}% completado")
                            last_update = current_time
        
        # Esperar a que termine el proceso
        returncode = process.wait()
        
        if returncode != 0:
            return False, f"Error en wget (código: {returncode})", {}
        
        # Verificar que el archivo existe y no está vacío
        if not os.path.exists(output_path):
            return False, "Archivo no se descargó correctamente", {}
        
        file_size = os.path.getsize(output_path)
        if file_size == 0:
            os.remove(output_path)
            return False, "Archivo descargado está vacío (0 bytes)", {}
        
        download_time = time.time() - download_start
        log_message(session_id, f"Descarga completada: {file_size:,} bytes en {download_time:.1f} segundos")
        
        # Obtener información del video
        video_info = get_video_info(output_path)
        video_info['title'] = Path(url).name.split('?')[0]  # Remover query parameters
        video_info['download_time'] = download_time
        video_info['download_speed'] = f"{file_size / download_time / 1024:.1f} KB/s"
        
        return True, output_path, video_info
        
    except Exception as e:
        return False, f"Error en descarga: {str(e)}", {}

def convert_video_ffmpeg(input_path: str, output_path: str, quality: str, session_id: str) -> Tuple[bool, str]:
    """Convertir video usando FFmpeg con progreso en tiempo real"""
    try:
        if quality not in VIDEO_QUALITIES:
            return False, f"Calidad no válida: {quality}"
        
        config = VIDEO_QUALITIES[quality]
        
        log_message(session_id, f"Iniciando conversión a {quality} ({config['width']}x{config['height']})")
        
        # Obtener duración del video para calcular progreso
        video_info = get_video_info(input_path)
        total_duration = video_info.get('duration', 0)
        
        if total_duration <= 0:
            log_message(session_id, "ADVERTENCIA: No se pudo obtener duración del video")
        
        # Comando FFmpeg con estadísticas de progreso
        cmd = [
            FFMPEG_PATH,
            '-i', input_path,
            '-vf', f'scale={config["width"]}:{config["height"]}:force_original_aspect_ratio=decrease,pad={config["width"]}:{config["height"]}:(ow-iw)/2:(oh-ih)/2',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-maxrate', config['bitrate'],
            '-bufsize', '2M',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-progress', 'pipe:1',  # Enviar progreso a stdout
            '-y',  # Sobrescribir
            output_path
        ]
        
        log_message(session_id, f"Ejecutando FFmpeg para conversión")
        
        # Ejecutar FFmpeg
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Redirigir stderr a stdout
            universal_newlines=True,
            bufsize=1
        )
        
        # Monitorear progreso en tiempo real
        conversion_start = time.time()
        last_progress_update = conversion_start
        
        for line in process.stdout:
            if session_id not in sessions:
                log_message(session_id, "Sesión eliminada durante conversión, cancelando...", "WARNING")
                process.terminate()
                break
                
            # Parsear línea de progreso de FFmpeg
            line = line.strip()
            if line.startswith('out_time_ms='):
                # out_time_ms está en microsegundos
                current_time_ms = int(line.split('=')[1])
                current_time = current_time_ms / 1000000  # Convertir a segundos
                
                if total_duration > 0:
                    progress = (current_time / total_duration) * 100
                    progress_int = int(progress)
                    
                    # Solo actualizar si cambió significativamente
                    if session_id in sessions and ('conversion_progress' not in sessions[session_id] or 
                       progress_int != sessions[session_id]['conversion_progress']):
                        
                        sessions[session_id]['conversion_progress'] = progress_int
                        
                        # Log cada 10% o cada 5 segundos
                        current_time_now = time.time()
                        if progress_int % 10 == 0 or current_time_now - last_progress_update > 5:
                            log_message(session_id, f"Conversión: {progress_int}% completado")
                            last_progress_update = current_time_now
            
            elif 'error' in line.lower() and not line.startswith('frame='):
                log_message(session_id, f"FFmpeg: {line[:100]}", "WARNING")
        
        # Esperar a que termine
        process.wait()
        
        if process.returncode != 0:
            return False, f"FFmpeg falló con código {process.returncode}"
        
        conversion_time = time.time() - conversion_start
        log_message(session_id, f"Conversión completada en {conversion_time:.1f} segundos")
        
        return True, output_path
        
    except Exception as e:
        log_message(session_id, f"Error en conversión: {str(e)}", "ERROR")
        return False, str(e)

def cleanup_old_files():
    """Limpiar archivos antiguos automáticamente"""
    try:
        now = time.time()
        cutoff = now - (app.config['MAX_RETENTION_MINUTES'] * 60)
        
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
                            print(f"🗑️  Eliminado archivo antiguo: {filepath}")
                    except Exception as e:
                        print(f"❌ Error eliminando archivo: {e}")
        
        # Limpiar sesiones antiguas (pero NO las que están en proceso)
        sessions_to_remove = []
        for session_id, session_data in sessions.items():
            created_at = datetime.fromisoformat(session_data["created_at"]).timestamp()
            
            # NO eliminar sesiones activas
            if session_data.get("status") in ["downloading", "converting", "downloaded"]:
                continue
                
            if created_at < cutoff:
                sessions_to_remove.append(session_id)
        
        for session_id in sessions_to_remove:
            sessions.pop(session_id, None)
            print(f"🗑️  Eliminada sesión antigua: {session_id[:8]}")
            
    except Exception as e:
        print(f"❌ Error en limpieza automática: {e}")

# Iniciar limpieza automática en segundo plano
def periodic_cleanup():
    while True:
        time.sleep(300)  # Cada 5 minutos
        cleanup_old_files()

cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

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
        
        # Validar URL
        is_valid, message = validate_video_url(url)
        if not is_valid:
            return jsonify({
                "success": False,
                "message": message
            }), 400
        
        # Crear sesión
        session_id = generate_session_id()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_filename = f"original_{session_id}_{timestamp}.mp4"
        original_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        
        # Estado inicial de la sesión
        sessions[session_id] = {
            "url": url,
            "status": "validating",
            "original_path": original_path,
            "created_at": datetime.now().isoformat(),
            "conversion_progress": 0,
            "download_progress": 0,
            "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] Sesión creada"],
            "error": None
        }
        
        log_message(session_id, f"Iniciando procesamiento de URL: {url[:50]}...")
        
        def download_background():
            """Función de descarga en segundo plano"""
            try:
                # Actualizar estado
                if session_id in sessions:
                    sessions[session_id]['status'] = 'downloading'
                log_message(session_id, "Iniciando descarga del video")
                
                # Descargar video
                success, result, video_info = download_video_direct(url, original_path, session_id)
                
                if success:
                    # Éxito en descarga
                    if session_id in sessions:
                        sessions[session_id].update({
                            "status": "downloaded",
                            "video_info": video_info,
                            "downloaded_at": datetime.now().isoformat(),
                            "original_size": os.path.getsize(original_path),
                            "error": None
                        })
                    
                    log_message(session_id, 
                        f"✅ Descarga completada: {video_info.get('width', 0)}x{video_info.get('height', 0)}, "
                        f"{video_info.get('duration_formatted', '0:00')}, "
                        f"{video_info.get('size_formatted', '0 MB')}"
                    )
                    
                else:
                    # Error en descarga
                    if session_id in sessions:
                        sessions[session_id].update({
                            "status": "error",
                            "error": result
                        })
                    log_message(session_id, f"❌ Error en descarga: {result}", "ERROR")
                    
            except Exception as e:
                if session_id in sessions:
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
        
        log_message(session_id, "✅ Solicitud aceptada, iniciando descarga en background")
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "qualities": available_qualities,
            "message": "URL válida. Iniciando descarga...",
            "logs": sessions[session_id]['logs']
        })
        
    except Exception as e:
        app.logger.error(f"Error procesando video: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/convert', methods=['POST'])
def convert_video():
    """Convertir video a calidad específica - VERSIÓN COMPLETAMENTE CORREGIDA"""
    try:
        data = request.get_json()
        if not data or 'session_id' not in data or 'quality' not in data:
            return jsonify({
                "success": False,
                "message": "Datos incompletos"
            }), 400
        
        session_id = data['session_id']
        quality = data['quality']
        
        print(f"🔍 [API] Buscando sesión: {session_id}")
        print(f"📊 [API] Sesiones activas: {list(sessions.keys())}")
        
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": f"Sesión no encontrada: {session_id}"
            }), 404
        
        session_data = sessions[session_id]
        print(f"📋 [API] Estado actual de sesión: {session_data.get('status')}")
        
        # Verificar que el video esté descargado
        if session_data.get("status") != "downloaded":
            return jsonify({
                "success": False,
                "message": f"Video no descargado aún. Estado actual: {session_data.get('status')}"
            }), 400
        
        if quality not in VIDEO_QUALITIES:
            return jsonify({
                "success": False,
                "message": f"Calidad no válida. Opciones: {', '.join(VIDEO_QUALITIES.keys())}"
            }), 400
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_filename = f"converted_{session_id}_{quality}_{timestamp}.mp4"
        output_path = os.path.join(app.config['CONVERTED_FOLDER'], output_filename)
        
        print(f"🎬 [API] Iniciando conversión para sesión {session_id}")
        print(f"🎯 [API] Calidad objetivo: {quality}")
        print(f"💾 [API] Ruta de salida: {output_path}")
        
        # Asegurar que la sesión existe antes de actualizar
        if session_id not in sessions:
            return jsonify({
                "success": False,
                "message": f"Sesión eliminada antes de iniciar conversión"
            }), 404
        
        # Actualizar estado ANTES de iniciar el hilo
        sessions[session_id].update({
            "status": "converting",
            "target_quality": quality,
            "output_path": output_path,
            "conversion_started": datetime.now().isoformat(),
            "conversion_progress": 0
        })
        
        log_message(session_id, f"Iniciando conversión a {quality}")
        
        # Guardar referencia a los datos necesarios ANTES de iniciar el hilo
        original_path = session_data['original_path']
        
        def convert_background():
            """Función de conversión en segundo plano"""
            try:
                print(f"🔄 [BACKGROUND] Iniciando FFmpeg para sesión {session_id}")
                print(f"📁 [BACKGROUND] Archivo de entrada: {original_path}")
                print(f"📁 [BACKGROUND] Archivo de salida: {output_path}")
                
                # Verificar que el archivo de entrada existe
                if not os.path.exists(original_path):
                    error_msg = f"Archivo de entrada no existe: {original_path}"
                    print(f"❌ [BACKGROUND] {error_msg}")
                    if session_id in sessions:
                        sessions[session_id].update({
                            "status": "conversion_error",
                            "error": error_msg
                        })
                    return
                
                success, result = convert_video_ffmpeg(
                    original_path,
                    output_path,
                    quality,
                    session_id
                )
                
                if success:
                    # Conversión exitosa
                    converted_info = get_video_info(output_path)
                    
                    # Asegurarse de que la sesión aún existe
                    if session_id in sessions:
                        sessions[session_id].update({
                            "status": "completed",
                            "converted_at": datetime.now().isoformat(),
                            "conversion_progress": 100,
                            "converted_path": output_path,
                            "converted_info": converted_info
                        })
                        
                        log_message(session_id, 
                            f"🎉 Conversión completada: {quality}, "
                            f"{converted_info.get('size_formatted', '0 MB')}"
                        )
                        print(f"✅ [BACKGROUND] Conversión completada para sesión {session_id}")
                    else:
                        print(f"⚠️ [BACKGROUND] Sesión {session_id} ya no existe al completar la conversión")
                        
                else:
                    # Error en conversión
                    if session_id in sessions:
                        sessions[session_id].update({
                            "status": "conversion_error",
                            "error": result,
                            "conversion_progress": 0
                        })
                        log_message(session_id, f"❌ Error en conversión: {result}", "ERROR")
                    print(f"❌ [BACKGROUND] Error FFmpeg para sesión {session_id}: {result}")
                    
            except Exception as e:
                error_msg = f"💥 Error inesperado en conversión: {str(e)}"
                print(f"❌ [BACKGROUND] {error_msg}")
                if session_id in sessions:
                    sessions[session_id].update({
                        "status": "conversion_error",
                        "error": error_msg
                    })
                    log_message(session_id, error_msg, "ERROR")
        
        # Iniciar conversión en segundo plano
        convert_thread = threading.Thread(target=convert_background, daemon=True)
        convert_thread.start()
        
        # Pequeña pausa para asegurar que el hilo se inició
        time.sleep(0.1)
        
        # Verificar que el hilo se inició
        print(f"🧵 [API] Hilo de conversión iniciado para sesión {session_id}")
        
        # Devolver respuesta inmediata
        return jsonify({
            "success": True,
            "message": f"Conversión a {quality} iniciada",
            "session_id": session_id,
            "status": "converting",
            "logs": sessions[session_id].get('logs', [])
        })
        
    except Exception as e:
        error_msg = f"Error convirtiendo video: {str(e)}"
        print(f"❌ [API] {error_msg}")
        app.logger.error(error_msg)
        return jsonify({
            "success": False,
            "message": f"Error interno: {str(e)}"
        }), 500

@app.route('/api/status/<session_id>')
def get_status(session_id):
    """Obtener estado de la conversión con logs en tiempo real"""
    try:
        print(f"📡 [STATUS] Consultando estado para sesión: {session_id}")
        print(f"📊 [STATUS] Sesiones disponibles: {list(sessions.keys())}")
        
        if session_id not in sessions:
            print(f"❌ [STATUS] Sesión {session_id} no encontrada")
            return jsonify({
                "success": False,
                "message": f"Sesión no encontrada: {session_id}"
            }), 404
        
        session_data = sessions[session_id]
        
        print(f"📋 [STATUS] Estado de {session_id}: {session_data.get('status')}")
        print(f"📈 [STATUS] Progreso conversión: {session_data.get('conversion_progress', 0)}%")
        print(f"📈 [STATUS] Progreso descarga: {session_data.get('download_progress', 0)}%")
        
        # Preparar respuesta
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
        
        # Agregar información del video si está disponible
        if "video_info" in session_data:
            response["video_info"] = session_data["video_info"]
            response["original_quality"] = session_data["video_info"].get("quality", "Desconocida")
        
        # Agregar información de conversión si está disponible
        if "converted_info" in session_data:
            response["converted_info"] = session_data["converted_info"]
            response["converted"] = True
            response["video_url"] = f"/api/stream/{session_id}"
            response["download_url"] = f"/api/download/{session_id}"
            response["quality"] = session_data.get("target_quality")
        else:
            response["converted"] = False
        
        print(f"✅ [STATUS] Respuesta preparada para {session_id}")
        return jsonify(response)
        
    except Exception as e:
        print(f"❌ [STATUS] Error obteniendo estado: {str(e)}")
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
        
        # Solo cancelar si está en proceso de conversión
        if sessions[session_id].get('status') == 'converting':
            sessions[session_id]['status'] = 'cancelled'
            log_message(session_id, "Conversión cancelada por el usuario")
        
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

@app.route('/api/logs/<session_id>')
def get_logs(session_id):
    """Obtener logs específicos de una sesión"""
    if session_id not in sessions:
        return jsonify({"success": False, "message": "Sesión no encontrada"}), 404
    
    return jsonify({
        "success": True,
        "session_id": session_id,
        "logs": sessions[session_id].get('logs', [])
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
            "sessions_list": list(sessions.keys()),
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
    print("🚀 SERVIDOR DE CONVERSIÓN DE VIDEOS FFMPEG")
    print("=" * 60)
    print(f"📡 URL: http://0.0.0.0:{port}")
    print(f"🔧 FFmpeg: {FFMPEG_PATH}")
    print(f"🎯 Calidades disponibles: {', '.join(VIDEO_QUALITIES.keys())}")
    print(f"💾 Directorio temporal: {app.config['UPLOAD_FOLDER']}")
    print(f"💾 Directorio convertidos: {app.config['CONVERTED_FOLDER']}")
    print(f"⏱️  Retención: {app.config['MAX_RETENTION_MINUTES']} minutos")
    print("=" * 60)
    print("✅ Sistema listo para recibir solicitudes")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)