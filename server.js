const express = require('express');
const multer = require('multer');
const fs = require('fs-extra');
const path = require('path');
const cors = require('cors');
const http = require('http');
const socketIo = require('socket.io');
const sqlite3 = require('sqlite3').verbose();
const ffmpeg = require('fluent-ffmpeg');

const app = express();
const server = http.createServer(app);
const io = socketIo(server, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"]
  }
});

const PORT = process.env.PORT || 10000;

// Configuración para Render
const DATA_DIR = process.env.NODE_ENV === 'production' 
  ? '/data'  // En Render, usa el disco persistente
  : path.join(__dirname, 'data');

const UPLOADS_DIR = path.join(DATA_DIR, 'uploads');
const THUMBS_DIR = path.join(DATA_DIR, 'thumbnails');
const DB_PATH = path.join(DATA_DIR, 'videos.db');

// Asegurar que los directorios existen
fs.ensureDirSync(DATA_DIR);
fs.ensureDirSync(UPLOADS_DIR);
fs.ensureDirSync(THUMBS_DIR);

// Inicializar base de datos SQLite
const db = new sqlite3.Database(DB_PATH, (err) => {
  if (err) {
    console.error('Error abriendo base de datos:', err);
  } else {
    console.log('✅ Conectado a SQLite:', DB_PATH);
    
    // Crear tablas
    db.run(`
      CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        filename TEXT NOT NULL UNIQUE,
        original_name TEXT NOT NULL,
        filepath TEXT NOT NULL,
        thumbnail_path TEXT,
        duration INTEGER DEFAULT 0,
        size INTEGER NOT NULL,
        views INTEGER DEFAULT 0,
        uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
      )
    `, (err) => {
      if (err) console.error('Error creando tabla videos:', err);
      else console.log('✅ Tabla videos creada/verificada');
    });
  }
});

// Configuración de Multer
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    cb(null, UPLOADS_DIR);
  },
  filename: (req, file, cb) => {
    const uniqueName = `video_${Date.now()}_${Math.random().toString(36).substring(7)}${path.extname(file.originalname)}`;
    cb(null, uniqueName);
  }
});

const upload = multer({
  storage: storage,
  limits: { fileSize: 100 * 1024 * 1024 }, // 100MB
  fileFilter: (req, file, cb) => {
    const allowedMimes = [
      'video/mp4',
      'video/webm',
      'video/ogg',
      'video/quicktime',
      'video/x-msvideo',
      'video/x-matroska'
    ];
    
    if (allowedMimes.includes(file.mimetype)) {
      cb(null, true);
    } else {
      cb(new Error('Solo se permiten archivos de video.'));
    }
  }
});

// Middleware
app.use(cors());
app.use(express.json());
app.use('/uploads', express.static(UPLOADS_DIR));
app.use('/thumbnails', express.static(THUMBS_DIR));

// Función para procesar video con FFmpeg
function processVideoWithFFmpeg(videoPath, videoId) {
  return new Promise((resolve, reject) => {
    // Extraer duración
    ffmpeg.ffprobe(videoPath, (err, metadata) => {
      if (err) {
        console.error('Error en ffprobe:', err);
        // Si falla, usar valores por defecto
        resolve({ duration: 0, thumbnailPath: null });
        return;
      }
      
      const duration = Math.floor(metadata.format.duration || 0);
      const thumbnailFilename = `${videoId}.jpg`;
      const thumbnailPath = path.join(THUMBS_DIR, thumbnailFilename);
      
      console.log(`📹 Procesando video ID ${videoId}: duración=${duration}s`);
      
      // Extraer miniatura en el segundo 2 (5% puede ser muy temprano)
      ffmpeg(videoPath)
        .screenshots({
          timestamps: ['2'],
          filename: thumbnailFilename,
          folder: THUMBS_DIR,
          size: '400x225'
        })
        .on('end', () => {
          console.log(`✅ Miniatura generada: ${thumbnailFilename}`);
          resolve({ duration, thumbnailPath });
        })
        .on('error', (err) => {
          console.error('Error generando miniatura:', err);
          // Aún resolvemos aunque falle la miniatura
          resolve({ duration, thumbnailPath: null });
        });
    });
  });
}

// Helper para consultas con promesas
function dbAll(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.all(sql, params, (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });
}

function dbRun(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.run(sql, params, function(err) {
      if (err) reject(err);
      else resolve({ lastID: this.lastID, changes: this.changes });
    });
  });
}

function dbGet(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.get(sql, params, (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });
}

// API: Obtener todos los videos
app.get('/api/videos', async (req, res) => {
  try {
    const videos = await dbAll('SELECT * FROM videos ORDER BY uploaded_at DESC');
    
    const formattedVideos = videos.map(video => ({
      id: video.id,
      title: video.title,
      filename: video.filename,
      url: `/uploads/${video.filename}`,
      thumbnail: video.thumbnail_path ? `/thumbnails/${path.basename(video.thumbnail_path)}` : null,
      duration: formatDuration(video.duration),
      size: formatFileSize(video.size),
      views: video.views,
      uploaded_at: video.uploaded_at
    }));
    
    res.json(formattedVideos);
  } catch (error) {
    console.error('Error obteniendo videos:', error);
    res.status(500).json({ error: 'Error del servidor' });
  }
});

// API: Subir video
app.post('/api/upload', upload.single('video'), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: 'No se recibió archivo' });
    }
    
    // Usar nombre del archivo como título (sin extensión)
    const title = req.file.originalname.replace(/\.[^/.]+$/, '');
    const videoPath = req.file.path;
    
    console.log(`⬆️  Subiendo video: ${title} (${formatFileSize(req.file.size)})`);
    
    // Insertar video en la base de datos
    const result = await dbRun(
      `INSERT INTO videos (title, filename, original_name, filepath, size) 
       VALUES (?, ?, ?, ?, ?)`,
      [title, req.file.filename, req.file.originalname, videoPath, req.file.size]
    );
    
    const videoId = result.lastID;
    
    // Notificar subida inmediata vía WebSocket
    io.emit('video-uploaded', {
      id: videoId,
      title: title,
      size: formatFileSize(req.file.size),
      uploaded_at: new Date().toISOString()
    });
    
    // Procesar con FFmpeg en segundo plano (no bloquear respuesta)
    setTimeout(async () => {
      try {
        const { duration, thumbnailPath } = await processVideoWithFFmpeg(videoPath, videoId);
        
        // Actualizar video con duración y miniatura
        await dbRun(
          'UPDATE videos SET duration = ?, thumbnail_path = ? WHERE id = ?',
          [duration, thumbnailPath, videoId]
        );
        
        console.log(`✅ Video ${videoId} procesado: ${duration}s`);
        
        // Notificar que el video fue procesado
        io.emit('video-processed', {
          videoId,
          duration,
          thumbnail: thumbnailPath ? `/thumbnails/${videoId}.jpg` : null
        });
        
      } catch (error) {
        console.error('Error procesando video en segundo plano:', error);
      }
    }, 1000);
    
    res.json({
      success: true,
      message: 'Video subido exitosamente',
      videoId: videoId,
      filename: req.file.filename,
      title: title
    });
    
  } catch (error) {
    console.error('Error subiendo video:', error);
    
    // Si hay error, eliminar el archivo subido
    if (req.file && fs.existsSync(req.file.path)) {
      fs.unlinkSync(req.file.path);
    }
    
    res.status(500).json({ 
      error: 'Error al subir el video',
      details: error.message 
    });
  }
});

// API: Obtener video específico
app.get('/api/videos/:id', async (req, res) => {
  try {
    const video = await dbGet('SELECT * FROM videos WHERE id = ?', [req.params.id]);
    
    if (!video) {
      return res.status(404).json({ error: 'Video no encontrado' });
    }
    
    // Incrementar vistas
    await dbRun('UPDATE videos SET views = views + 1 WHERE id = ?', [video.id]);
    
    res.json({
      id: video.id,
      title: video.title,
      filename: video.filename,
      url: `/uploads/${video.filename}`,
      thumbnail: video.thumbnail_path ? `/thumbnails/${path.basename(video.thumbnail_path)}` : null,
      duration: formatDuration(video.duration),
      size: formatFileSize(video.size),
      views: video.views + 1,
      uploaded_at: video.uploaded_at,
      original_name: video.original_name
    });
  } catch (error) {
    console.error('Error obteniendo video:', error);
    res.status(500).json({ error: 'Error del servidor' });
  }
});

// API: Descargar video
app.get('/api/videos/:id/download', async (req, res) => {
  try {
    const video = await dbGet('SELECT * FROM videos WHERE id = ?', [req.params.id]);
    
    if (!video) {
      return res.status(404).json({ error: 'Video no encontrado' });
    }
    
    if (!fs.existsSync(video.filepath)) {
      return res.status(404).json({ error: 'Archivo no encontrado' });
    }
    
    // Enviar archivo para descarga
    res.download(video.filepath, video.original_name, (err) => {
      if (err) {
        console.error('Error descargando video:', err);
        res.status(500).json({ error: 'Error descargando archivo' });
      }
    });
    
  } catch (error) {
    console.error('Error en descarga:', error);
    res.status(500).json({ error: 'Error del servidor' });
  }
});

// API: Servir archivo HTML
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// Servir archivos estáticos
app.use(express.static('public'));

// WebSocket para tiempo real
io.on('connection', (socket) => {
  console.log('🔌 Cliente conectado:', socket.id);
  
  socket.on('upload-progress', (data) => {
    // Broadcast a otros clientes
    socket.broadcast.emit('upload-progress', data);
  });
  
  socket.on('video-playing', (videoId) => {
    io.emit('video-playing', { videoId, time: new Date().toISOString() });
  });
  
  socket.on('disconnect', () => {
    console.log('🔌 Cliente desconectado:', socket.id);
  });
});

// Funciones de utilidad
function formatDuration(seconds) {
  if (!seconds || seconds === 0) return '0:00';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatFileSize(bytes) {
  if (!bytes) return '0 Bytes';
  const k = 1024;
  const sizes = ['Bytes', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Iniciar servidor
server.listen(PORT, () => {
  console.log(`🚀 Servidor ejecutándose en http://localhost:${PORT}`);
  console.log(`📁 Videos: ${UPLOADS_DIR}`);
  console.log(`🖼️  Miniaturas: ${THUMBS_DIR}`);
  console.log(`🗄️  Base de datos: ${DB_PATH}`);
  
  // Verificar que FFmpeg esté disponible
  ffmpeg.getAvailableCodecs((err, codecs) => {
    if (err) {
      console.error('❌ FFmpeg no disponible. Las miniaturas no se generarán.');
      console.error('Error FFmpeg:', err.message);
    } else {
      console.log('✅ FFmpeg disponible para procesamiento de videos');
    }
  });
});

// Manejar cierre limpio
process.on('SIGINT', () => {
  console.log('Apagando servidor...');
  db.close();
  process.exit(0);
});