const express = require('express');
const multer = require('multer');
const fs = require('fs-extra');
const path = require('path');
const cors = require('cors');
const http = require('http');
const socketIo = require('socket.io');
const Database = require('better-sqlite3');
const ffmpeg = require('fluent-ffmpeg');

const app = express();
const server = http.createServer(app);
const io = socketIo(server);

const PORT = process.env.PORT || 10000;

// Directorios persistentes en Render
const DATA_DIR = process.env.NODE_ENV === 'production' ? '/data' : path.join(__dirname, 'data');
const UPLOADS_DIR = path.join(DATA_DIR, 'uploads');
const THUMBS_DIR = path.join(DATA_DIR, 'thumbnails');
const DB_PATH = path.join(DATA_DIR, 'videos.db');

// Crear directorios necesarios
fs.ensureDirSync(DATA_DIR);
fs.ensureDirSync(UPLOADS_DIR);
fs.ensureDirSync(THUMBS_DIR);

// Inicializar SQLite
const db = new Database(DB_PATH);

db.exec(`
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
  );
`);

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
  limits: { fileSize: 100 * 1024 * 1024 }
});

// Middleware
app.use(cors());
app.use(express.json());
app.use('/uploads', express.static(UPLOADS_DIR));
app.use('/thumbnails', express.static(THUMBS_DIR));

// Función para extraer duración y miniatura con FFmpeg
function processVideoWithFFmpeg(videoPath, videoId) {
  return new Promise((resolve, reject) => {
    // Extraer duración
    ffmpeg.ffprobe(videoPath, (err, metadata) => {
      if (err) return reject(err);
      
      const duration = Math.floor(metadata.format.duration || 0);
      const thumbnailPath = path.join(THUMBS_DIR, `${videoId}.jpg`);
      
      // Extraer miniatura en el segundo 5
      ffmpeg(videoPath)
        .screenshots({
          timestamps: ['5%'],
          filename: `${videoId}.jpg`,
          folder: THUMBS_DIR,
          size: '400x225'
        })
        .on('end', () => resolve({ duration, thumbnailPath }))
        .on('error', reject);
    });
  });
}

// API: Obtener todos los videos
app.get('/api/videos', (req, res) => {
  try {
    const videos = db.prepare('SELECT * FROM videos ORDER BY uploaded_at DESC').all();
    
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
    console.error('Error:', error);
    res.status(500).json({ error: 'Error del servidor' });
  }
});

// API: Subir video
app.post('/api/upload', upload.single('video'), async (req, res) => {
  try {
    if (!req.file) return res.status(400).json({ error: 'No se recibió archivo' });
    
    const title = req.file.originalname.replace(/\.[^/.]+$/, '');
    const videoPath = req.file.path;
    
    // Insertar video inicialmente
    const stmt = db.prepare(`
      INSERT INTO videos (title, filename, original_name, filepath, size)
      VALUES (?, ?, ?, ?, ?)
    `);
    
    const result = stmt.run(
      title,
      req.file.filename,
      req.file.originalname,
      videoPath,
      req.file.size
    );
    
    const videoId = result.lastInsertRowid;
    
    // Procesar con FFmpeg en segundo plano
    processVideoWithFFmpeg(videoPath, videoId)
      .then(({ duration, thumbnailPath }) => {
        db.prepare('UPDATE videos SET duration = ?, thumbnail_path = ? WHERE id = ?')
          .run(duration, thumbnailPath, videoId);
        
        io.emit('video-processed', { videoId, duration, thumbnail: `/thumbnails/${videoId}.jpg` });
      })
      .catch(err => console.error('Error procesando video:', err));
    
    // Notificar subida inmediata vía WebSocket
    io.emit('video-uploaded', {
      id: videoId,
      title: title,
      size: formatFileSize(req.file.size),
      uploaded_at: new Date().toISOString()
    });
    
    res.json({
      success: true,
      message: 'Video subido, procesando...',
      videoId: videoId,
      filename: req.file.filename
    });
    
  } catch (error) {
    console.error('Error subiendo video:', error);
    res.status(500).json({ error: 'Error subiendo video' });
  }
});

// API: Reproducir video específico
app.get('/api/videos/:id', (req, res) => {
  try {
    const video = db.prepare('SELECT * FROM videos WHERE id = ?').get(req.params.id);
    
    if (!video) return res.status(404).json({ error: 'Video no encontrado' });
    
    // Incrementar vistas
    db.prepare('UPDATE videos SET views = views + 1 WHERE id = ?').run(video.id);
    
    res.json({
      id: video.id,
      title: video.title,
      filename: video.filename,
      url: `/uploads/${video.filename}`,
      thumbnail: video.thumbnail_path ? `/thumbnails/${path.basename(video.thumbnail_path)}` : null,
      duration: formatDuration(video.duration),
      size: formatFileSize(video.size),
      views: video.views + 1,
      uploaded_at: video.uploaded_at
    });
  } catch (error) {
    console.error('Error:', error);
    res.status(500).json({ error: 'Error del servidor' });
  }
});

// API: Descargar video
app.get('/api/videos/:id/download', (req, res) => {
  try {
    const video = db.prepare('SELECT * FROM videos WHERE id = ?').get(req.params.id);
    
    if (!video) return res.status(404).json({ error: 'Video no encontrado' });
    
    if (!fs.existsSync(video.filepath)) {
      return res.status(404).json({ error: 'Archivo no encontrado' });
    }
    
    res.download(video.filepath, video.original_name);
  } catch (error) {
    console.error('Error:', error);
    res.status(500).json({ error: 'Error del servidor' });
  }
});

// WebSocket para tiempo real
io.on('connection', (socket) => {
  console.log('Cliente conectado');
  
  socket.on('upload-progress', (data) => {
    socket.broadcast.emit('upload-progress', data);
  });
  
  socket.on('video-playing', (videoId) => {
    io.emit('video-playing', { videoId, time: new Date().toISOString() });
  });
  
  socket.on('disconnect', () => {
    console.log('Cliente desconectado');
  });
});

// Servir archivos estáticos y HTML
app.use(express.static('public'));
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// Funciones de utilidad
function formatDuration(seconds) {
  if (!seconds) return '0:00';
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
  console.log(`🚀 Servidor en http://localhost:${PORT}`);
  console.log(`📁 Videos: ${UPLOADS_DIR}`);
  console.log(`🖼️  Miniaturas: ${THUMBS_DIR}`);
  console.log(`🗄️  Base de datos: ${DB_PATH}`);
});