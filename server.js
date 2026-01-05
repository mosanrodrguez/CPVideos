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

// 📁 DIRECTORIOS TEMPORALES DENTRO DEL PROYECTO
const PROJECT_DIR = __dirname;
const PUBLIC_DIR = path.join(PROJECT_DIR, 'public');
const UPLOADS_DIR = path.join(PROJECT_DIR, 'temp_uploads');
const THUMBS_DIR = path.join(PROJECT_DIR, 'temp_thumbnails');
const DB_PATH = path.join(PROJECT_DIR, 'temp_videos.db');

console.log('='.repeat(60));
console.log('🚀 PLATAFORMA DE VIDEOS - INICIANDO');
console.log('='.repeat(60));
console.log('📂 Directorio del proyecto:', PROJECT_DIR);
console.log('🌐 Directorio público:', PUBLIC_DIR);
console.log('💾 Uploads temporales:', UPLOADS_DIR);
console.log('🖼️  Thumbnails temporales:', THUMBS_DIR);
console.log('🗄️  Base de datos:', DB_PATH);
console.log('🔌 Puerto:', PORT);
console.log('='.repeat(60));

// Verificar que existe la carpeta public/
if (!fs.existsSync(PUBLIC_DIR)) {
  console.log('⚠️  Creando carpeta public/...');
  fs.mkdirSync(PUBLIC_DIR, { recursive: true });
}

// Crear directorios temporales si no existen
[UPLOADS_DIR, THUMBS_DIR].forEach(dir => {
  if (!fs.existsSync(dir)) {
    try {
      fs.mkdirSync(dir, { recursive: true });
      console.log(`✅ Directorio creado: ${dir}`);
    } catch (error) {
      console.error(`❌ Error creando ${dir}:`, error.message);
    }
  }
});

// Inicializar base de datos SQLite
const db = new sqlite3.Database(DB_PATH, (err) => {
  if (err) {
    console.error('❌ Error abriendo base de datos:', err.message);
  } else {
    console.log('✅ Base de datos SQLite conectada');
  }
});

// Crear tabla de videos
db.run(`
  CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    filepath TEXT NOT NULL,
    thumbnail_path TEXT,
    duration INTEGER DEFAULT 0,
    size INTEGER NOT NULL,
    views INTEGER DEFAULT 0,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )
`, (err) => {
  if (err) {
    console.error('❌ Error creando tabla videos:', err.message);
  } else {
    console.log('✅ Tabla videos lista');
  }
});

// Configuración de Multer
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    cb(null, UPLOADS_DIR);
  },
  filename: (req, file, cb) => {
    // Nombre seguro y único
    const safeName = file.originalname.replace(/[^a-zA-Z0-9._-]/g, '_');
    const uniqueName = `video_${Date.now()}_${Math.random().toString(36).substring(2, 9)}_${safeName}`;
    cb(null, uniqueName);
  }
});

const upload = multer({
  storage: storage,
  limits: {
    fileSize: 50 * 1024 * 1024, // 50MB
    files: 1
  }
});

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Servir archivos estáticos
app.use('/uploads', express.static(UPLOADS_DIR));
app.use('/thumbnails', express.static(THUMBS_DIR));
app.use(express.static(PUBLIC_DIR)); // Servir archivos estáticos de public/

// Función para procesar video con FFmpeg
function processVideoWithFFmpeg(videoPath, videoId) {
  return new Promise((resolve) => {
    console.log(`🔄 Procesando video ID ${videoId}...`);
    
    // Verificar si el archivo existe
    if (!fs.existsSync(videoPath)) {
      console.log(`⚠️  Archivo no encontrado: ${videoPath}`);
      resolve({ duration: 0, thumbnailPath: null });
      return;
    }
    
    // Extraer duración y miniatura
    ffmpeg.ffprobe(videoPath, (err, metadata) => {
      if (err) {
        console.log('⚠️  No se pudo analizar video:', err.message);
        resolve({ duration: 0, thumbnailPath: null });
        return;
      }
      
      const duration = Math.floor(metadata.format.duration || 0);
      const thumbnailFilename = `thumb_${videoId}.jpg`;
      const thumbnailPath = path.join(THUMBS_DIR, thumbnailFilename);
      
      // Generar miniatura
      ffmpeg(videoPath)
        .screenshots({
          timestamps: ['00:00:01'],
          filename: thumbnailFilename,
          folder: THUMBS_DIR,
          size: '400x225'
        })
        .on('end', () => {
          console.log(`✅ Miniatura generada: ${thumbnailFilename}`);
          resolve({ duration, thumbnailPath });
        })
        .on('error', (err) => {
          console.log('⚠️  Error generando miniatura:', err.message);
          resolve({ duration, thumbnailPath: null });
        });
    });
  });
}

// Helper para consultas SQL
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

// ========== RUTAS DE LA API ==========

// Ruta raíz: servir index.html
app.get('/', (req, res) => {
  const indexPath = path.join(PUBLIC_DIR, 'index.html');
  if (fs.existsSync(indexPath)) {
    res.sendFile(indexPath);
  } else {
    res.send(`
      <!DOCTYPE html>
      <html>
      <head><title>Video Platform</title></head>
      <body>
        <h1>Video Platform - Servidor Funcionando</h1>
        <p>El servidor está corriendo en el puerto ${PORT}</p>
        <p>Pero el archivo index.html no se encontró en la carpeta public/</p>
        <p><a href="/api/videos">Ver API de videos</a></p>
      </body>
      </html>
    `);
  }
});

// API: Obtener todos los videos
app.get('/api/videos', async (req, res) => {
  try {
    const videos = await dbAll('SELECT * FROM videos ORDER BY uploaded_at DESC');
    
    const formattedVideos = videos.map(video => ({
      id: video.id,
      title: video.title,
      filename: video.filename,
      url: `/uploads/${video.filename}`,
      thumbnail: video.thumbnail_path 
        ? `/thumbnails/${path.basename(video.thumbnail_path)}`
        : `https://via.placeholder.com/400x225/d32f2f/ffffff?text=${encodeURIComponent(video.title.substring(0, 20))}`,
      duration: formatDuration(video.duration),
      size: formatFileSize(video.size),
      views: video.views || 0,
      uploaded_at: video.uploaded_at
    }));
    
    console.log(`📊 Enviando ${formattedVideos.length} videos`);
    res.json(formattedVideos);
  } catch (error) {
    console.error('Error obteniendo videos:', error);
    res.status(500).json({ error: 'Error interno' });
  }
});

// API: Subir video
app.post('/api/upload', upload.single('video'), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: 'No se recibió archivo' });
    }
    
    console.log(`📤 Subiendo video: ${req.file.originalname} (${formatFileSize(req.file.size)})`);
    
    // Título = nombre del archivo sin extensión
    const title = req.file.originalname.replace(/\.[^/.]+$/, '');
    
    // Guardar en base de datos
    const result = await dbRun(
      `INSERT INTO videos (title, filename, original_name, filepath, size) 
       VALUES (?, ?, ?, ?, ?)`,
      [title, req.file.filename, req.file.originalname, req.file.path, req.file.size]
    );
    
    const videoId = result.lastID;
    
    // Notificar vía WebSocket
    io.emit('video-uploaded', {
      id: videoId,
      title: title,
      size: formatFileSize(req.file.size)
    });
    
    // Procesar con FFmpeg en segundo plano
    setTimeout(async () => {
      try {
        const { duration, thumbnailPath } = await processVideoWithFFmpeg(req.file.path, videoId);
        
        await dbRun(
          'UPDATE videos SET duration = ?, thumbnail_path = ? WHERE id = ?',
          [duration, thumbnailPath, videoId]
        );
        
        console.log(`✅ Video ${videoId} procesado (${duration}s)`);
        io.emit('video-processed', { videoId, duration });
      } catch (error) {
        console.error('Error procesando video:', error);
      }
    }, 500);
    
    res.json({
      success: true,
      videoId: videoId,
      filename: req.file.filename,
      title: title,
      size: formatFileSize(req.file.size)
    });
    
  } catch (error) {
    console.error('Error subiendo video:', error);
    res.status(500).json({ error: error.message });
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
      thumbnail: video.thumbnail_path 
        ? `/thumbnails/${path.basename(video.thumbnail_path)}`
        : `https://via.placeholder.com/400x225/d32f2f/ffffff?text=Video`,
      duration: formatDuration(video.duration),
      size: formatFileSize(video.size),
      views: (video.views || 0) + 1,
      uploaded_at: video.uploaded_at
    });
  } catch (error) {
    console.error('Error obteniendo video:', error);
    res.status(500).json({ error: 'Error interno' });
  }
});

// API: Descargar video
app.get('/api/videos/:id/download', async (req, res) => {
  try {
    const video = await dbGet('SELECT * FROM videos WHERE id = ?', [req.params.id]);
    
    if (!video) {
      return res.status(404).json({ error: 'Video no encontrado' });
    }
    
    const filePath = path.join(UPLOADS_DIR, video.filename);
    
    if (!fs.existsSync(filePath)) {
      return res.status(404).json({ error: 'Archivo no encontrado' });
    }
    
    res.download(filePath, video.original_name);
  } catch (error) {
    console.error('Error descargando video:', error);
    res.status(500).json({ error: 'Error interno' });
  }
});

// Health check
app.get('/api/health', (req, res) => {
  res.json({
    status: 'OK',
    server: 'Video Platform',
    port: PORT,
    time: new Date().toISOString(),
    uploads: UPLOADS_DIR,
    public: PUBLIC_DIR
  });
});

// WebSocket para tiempo real
io.on('connection', (socket) => {
  console.log('🔌 Cliente conectado:', socket.id);
  
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
  console.log('='.repeat(60));
  console.log(`✅ SERVIDOR ACTIVO: http://localhost:${PORT}`);
  console.log('='.repeat(60));
  console.log('📂 Estructura de archivos:');
  console.log(`   public/: ${fs.existsSync(PUBLIC_DIR) ? '✅' : '❌'}`);
  console.log(`   temp_uploads/: ${fs.existsSync(UPLOADS_DIR) ? '✅' : '❌'}`);
  console.log(`   temp_thumbnails/: ${fs.existsSync(THUMBS_DIR) ? '✅' : '❌'}`);
  console.log(`   temp_videos.db: ${fs.existsSync(DB_PATH) ? '✅' : '❌'}`);
  console.log('='.repeat(60));
  console.log('🚀 Listo para recibir conexiones');
  console.log('='.repeat(60));
});

// Manejar cierre
process.on('SIGINT', () => {
  console.log('🛑 Apagando servidor...');
  db.close();
  process.exit(0);
});