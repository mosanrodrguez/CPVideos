// server.js - CPVideos Server con WebSockets para Chat en Tiempo Real
const express = require('express');
const multer = require('multer');
const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const fs = require('fs');
const bcrypt = require('bcrypt');
const cors = require('cors');
const jwt = require('jsonwebtoken');  // <-- AÑADE ESTA LÍNEA
const { spawn } = require('child_process');
const WebSocket = require('ws');

const app = express();
const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'cpvideos_secret_key_2024_change_this';

// ============================================
// CONFIGURACIÓN
// ============================================

const isProduction = process.env.NODE_ENV === 'production';
const BASE_UPLOAD_DIR = isProduction ? '/tmp' : __dirname;

// Directorios
const UPLOADS_DIR = path.join(BASE_UPLOAD_DIR, 'uploads');
const CHAT_MEDIA_DIR = path.join(UPLOADS_DIR, 'chat_media');
const VIDEOS_DIR = path.join(BASE_UPLOAD_DIR, 'videos');
const THUMBNAILS_DIR = path.join(BASE_UPLOAD_DIR, 'thumbnails');
const PUBLIC_DIR = path.join(__dirname, 'public');

// Crear directorios
function createDirectories() {
  const dirs = [UPLOADS_DIR, CHAT_MEDIA_DIR, VIDEOS_DIR, THUMBNAILS_DIR, PUBLIC_DIR];

  dirs.forEach(dir => {
    try {
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true, mode: 0o755 });
        console.log(`✅ Directorio creado: ${dir}`);
      }
    } catch (error) {
      console.error(`❌ Error creando directorio ${dir}:`, error.message);
    }
  });
}

createDirectories();

// ============================================
// WEBSOCKET SERVER - CHAT EN TIEMPO REAL
// ============================================

const server = require('http').createServer(app);
const wss = new WebSocket.Server({ server, path: '/ws' });

// Almacenar conexiones activas
const activeConnections = new Map(); // userId -> ws
const onlineUsers = new Set(); // userIds en línea

// Manejar conexiones WebSocket
wss.on('connection', (ws, req) => {
  console.log('🔗 Nueva conexión WebSocket');

  let userId = null;
  let username = null;

  // Heartbeat
  const heartbeatInterval = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'HEARTBEAT', timestamp: Date.now() }));
    }
  }, 30000);

  // Manejar mensajes
  ws.on('message', async (message) => {
    try {
      const data = JSON.parse(message);

      switch(data.type) {
        case 'AUTH':
          if (data.token) {
            jwt.verify(data.token, JWT_SECRET, (err, user) => {
              if (err) {
                ws.send(JSON.stringify({ type: 'AUTH_ERROR', message: 'Token inválido' }));
                ws.close();
                return;
              }

              userId = user.id;
              username = user.username;

              activeConnections.set(userId, ws);
              onlineUsers.add(userId);

              // Notificar a todos sobre nuevo usuario en línea
              broadcastOnlineCount();

              ws.send(JSON.stringify({ 
                type: 'AUTH_SUCCESS', 
                user: user 
              }));

              console.log(`✅ Usuario autenticado: ${username} (${userId})`);
            });
          }
          break;

        case 'MESSAGE':
          if (userId && (data.content || data.media_type)) {
            const messageData = {
              type: 'NEW_MESSAGE',
              message: {
                id: Date.now() + Math.random(),
                user_id: userId,
                username: username,
                content: data.content || '',
                media_type: data.media_type,
                media_url: data.media_url,
                created_at: new Date().toISOString()
              }
            };

            // Guardar mensaje en DB
            saveChatMessage(messageData.message);

            // Broadcast a todos los usuarios
            broadcastToAll(messageData);
          }
          break;

        case 'TYPING':
          if (userId && username) {
            const typingData = {
              type: 'USER_TYPING',
              userId: userId,
              username: username,
              action: data.action || 'typing'
            };

            // Enviar a todos excepto al que está escribiendo
            broadcastToOthers(userId, typingData);
          }
          break;

        case 'STOPPED_TYPING':
          if (userId) {
            const stoppedTypingData = {
              type: 'USER_STOPPED_TYPING',
              userId: userId
            };

            // Enviar a todos excepto al que dejó de escribir
            broadcastToOthers(userId, stoppedTypingData);
          }
          break;

        case 'GET_ONLINE_COUNT':
          ws.send(JSON.stringify({
            type: 'ONLINE_COUNT',
            count: onlineUsers.size
          }));
          break;

        case 'GET_RECENT_MESSAGES':
          getRecentMessages(ws);
          break;
      }
    } catch (error) {
      console.error('❌ Error procesando mensaje WebSocket:', error);
    }
  });

  // Manejar cierre
  ws.on('close', () => {
    clearInterval(heartbeatInterval);

    if (userId) {
      activeConnections.delete(userId);
      onlineUsers.delete(userId);

      // Notificar a todos sobre usuario desconectado
      broadcastOnlineCount();

      console.log(`👋 Usuario desconectado: ${username} (${userId})`);
    }
  });

  ws.on('error', (error) => {
    console.error('❌ Error en WebSocket:', error);
  });
});

// Funciones de broadcast
function broadcastToAll(data) {
  const message = JSON.stringify(data);
  activeConnections.forEach((ws, userId) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(message);
    }
  });
}

function broadcastToOthers(senderId, data) {
  const message = JSON.stringify(data);
  activeConnections.forEach((ws, userId) => {
    if (userId !== senderId && ws.readyState === WebSocket.OPEN) {
      ws.send(message);
    }
  });
}

function broadcastOnlineCount() {
  const countData = {
    type: 'ONLINE_COUNT',
    count: onlineUsers.size
  };

  broadcastToAll(countData);
}

// ============================================
// BASE DE DATOS
// ============================================

const db = new sqlite3.Database('./database.db', (err) => {
  if (err) {
    console.error('❌ Error conectando a SQLite:', err.message);
  } else {
    console.log('✅ Conectado a SQLite');
    initializeDatabase();
  }
});

function initializeDatabase() {
  db.serialize(() => {
    // Tabla de usuarios
    db.run(`CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      name TEXT,
      email TEXT,
      avatar TEXT DEFAULT 'default_avatar.png',
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )`);

    // Tabla de vídeos
    db.run(`CREATE TABLE IF NOT EXISTS videos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      filename TEXT NOT NULL,
      video_path TEXT NOT NULL,
      thumbnail_path TEXT,
      user_id INTEGER NOT NULL,
      user_name TEXT NOT NULL,
      views INTEGER DEFAULT 0,
      duration INTEGER DEFAULT 0,
      size INTEGER,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )`);

    // Tabla de mensajes de chat
    db.run(`CREATE TABLE IF NOT EXISTS chat_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      username TEXT NOT NULL,
      content TEXT,
      media_type TEXT,
      media_url TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )`);

    // Índices
    db.run('CREATE INDEX IF NOT EXISTS idx_videos_user ON videos(user_id)');
    db.run('CREATE INDEX IF NOT EXISTS idx_chat_messages_time ON chat_messages(created_at)');

    // Usuario demo
    const demoPassword = 'demo';
    bcrypt.hash(demoPassword, 10, (err, hash) => {
      if (!err) {
        db.run(
          `INSERT OR IGNORE INTO users (username, password, name, email) VALUES (?, ?, ?, ?)`,
          ['demo', hash, 'Usuario Demo', '']
        );
      }
    });
  });
}

// ============================================
// MIDDLEWARES
// ============================================

app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(express.static(PUBLIC_DIR));

// Middleware de logs
app.use((req, res, next) => {
  console.log(`${new Date().toISOString()} - ${req.method} ${req.url}`);
  next();
});

// ============================================
// FUNCIÓN PARA EXTRAER MINIATURA Y DURACIÓN
// ============================================

function extractThumbnailAndDuration(videoPath, thumbnailPath) {
  return new Promise((resolve, reject) => {
    const ffprobe = spawn('ffprobe', [
      '-v', 'error',
      '-show_entries', 'format=duration',
      '-of', 'default=noprint_wrappers=1:nokey=1',
      videoPath
    ]);

    let duration = 0;
    let durationOutput = '';

    ffprobe.stdout.on('data', (data) => {
      durationOutput += data.toString();
    });

    ffprobe.on('close', (code) => {
      if (code === 0) {
        duration = Math.round(parseFloat(durationOutput));

        const ffmpeg = spawn('ffmpeg', [
          '-i', videoPath,
          '-ss', '00:00:01',
          '-vframes', '1',
          '-vf', 'scale=320:180',
          '-q:v', '2',
          thumbnailPath,
          '-y'
        ]);

        ffmpeg.on('close', (code) => {
          if (code === 0) {
            resolve({ thumbnailPath, duration });
          } else {
            reject(new Error(`FFmpeg falló con código ${code}`));
          }
        });

        ffmpeg.stderr.on('data', () => {});
      } else {
        reject(new Error(`FFprobe falló con código ${code}`));
      }
    });

    ffprobe.stderr.on('data', () => {});
  });
}

// ============================================
// MIDDLEWARE DE AUTENTICACIÓN JWT
// ============================================

function authenticateToken(req, res, next) {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];

  if (!token) {
    return res.status(401).json({ error: 'Token requerido' });
  }

  jwt.verify(token, JWT_SECRET, (err, user) => {
    if (err) {
      return res.status(403).json({ error: 'Token inválido' });
    }
    req.user = user;
    next();
  });
}

// ============================================
// CONFIGURACIÓN MULTER
// ============================================

const videoStorage = multer.diskStorage({
  destination: function (req, file, cb) {
    cb(null, VIDEOS_DIR);
  },
  filename: function (req, file, cb) {
    const uniqueName = `${Date.now()}-${Math.round(Math.random() * 1E9)}${path.extname(file.originalname)}`;
    cb(null, uniqueName);
  }
});

const chatMediaStorage = multer.diskStorage({
  destination: function (req, file, cb) {
    cb(null, CHAT_MEDIA_DIR);
  },
  filename: function (req, file, cb) {
    const uniqueName = `${Date.now()}-${Math.round(Math.random() * 1E9)}${path.extname(file.originalname)}`;
    cb(null, uniqueName);
  }
});

const uploadVideo = multer({ 
  storage: videoStorage,
  limits: { fileSize: 500 * 1024 * 1024 } // 500MB
});

const uploadChatMedia = multer({
  storage: chatMediaStorage,
  limits: { 
    fileSize: 50 * 1024 * 1024, // 50MB para chat
    files: 1
  }
});

// Servir archivos estáticos
app.use('/uploads/chat_media', express.static(CHAT_MEDIA_DIR));
app.use('/videos', express.static(VIDEOS_DIR));
app.use('/thumbnails', express.static(THUMBNAILS_DIR));

// ============================================
// FUNCIONES AUXILIARES
// ============================================

function buildFullUrl(path) {
    if (!path) return null;
    if (path.startsWith('http')) return path;

    const BASE_URL = isProduction 
        ? 'https://cpvideos.onrender.com' 
        : `http://localhost:${PORT}`;

    return path.startsWith('/') ? `${BASE_URL}${path}` : `${BASE_URL}/${path}`;
}

// ============================================
// RUTAS DE AUTENTICACIÓN
// ============================================

app.post('/api/login', (req, res) => {
  const { username, password } = req.body;

  db.get('SELECT * FROM users WHERE username = ?', [username], (err, user) => {
    if (err) return res.status(500).json({ error: err.message });
    if (!user) return res.status(401).json({ error: 'Usuario no encontrado' });

    bcrypt.compare(password, user.password, (err, result) => {
      if (err) return res.status(500).json({ error: err.message });
      if (!result) return res.status(401).json({ error: 'Contraseña incorrecta' });

      delete user.password;

      const token = jwt.sign({ 
        id: user.id, 
        username: user.username,
        name: user.name 
      }, JWT_SECRET, { expiresIn: '7d' });

      res.json({ 
        success: true, 
        token: token, 
        user: user, 
        message: 'Login exitoso' 
      });
    });
  });
});

app.post('/api/register', (req, res) => {
  const { username, password, name } = req.body;

  if (!username || !password) {
    return res.status(400).json({ error: 'Nombre y contraseña requeridos' });
  }

  bcrypt.hash(password, 10, (err, hash) => {
    if (err) return res.status(500).json({ error: 'Error encriptando contraseña' });

    db.run(
      `INSERT INTO users (username, password, name, email) VALUES (?, ?, ?, ?)`,
      [username, hash, name || username, ''],
      function(err) {
        if (err) {
          if (err.message.includes('UNIQUE constraint failed')) {
            return res.status(400).json({ error: 'Usuario ya existe' });
          }
          return res.status(500).json({ error: err.message });
        }

        db.get('SELECT id, username, name, email, avatar, created_at FROM users WHERE id = ?', 
          [this.lastID], (err, newUser) => {
            if (err) return res.status(500).json({ error: err.message });

            const token = jwt.sign({ 
              id: newUser.id, 
              username: newUser.username,
              name: newUser.name 
            }, JWT_SECRET, { expiresIn: '7d' });

            res.json({ 
              success: true, 
              token: token, 
              user: newUser, 
              message: 'Usuario registrado exitosamente' 
            });
          }
        );
      }
    );
  });
});

app.get('/api/users/verify', authenticateToken, (req, res) => {
  db.get('SELECT id, username, name, email, avatar, created_at FROM users WHERE id = ?', 
    [req.user.id], (err, user) => {
      if (err) return res.status(500).json({ error: err.message });
      if (!user) return res.status(404).json({ error: 'Usuario no encontrado' });

      res.json({ 
        success: true, 
        user: user,
        message: 'Token válido'
      });
    }
  );
});

// ============================================
// RUTAS DE VIDEOS
// ============================================

// 1. SUBIR VÍDEO
app.post('/api/videos/upload', authenticateToken, uploadVideo.single('video'), async (req, res) => {
  if (!req.file) {
    return res.status(400).json({ 
      success: false, 
      error: 'No se recibió ningún archivo de video' 
    });
  }

  const userId = req.user.id;
  const userName = req.user.name || req.user.username;

  const videoPath = `/videos/${req.file.filename}`;
  const videoFilename = req.file.filename;
  const thumbnailFilename = `${path.parse(videoFilename).name}.jpg`;
  const thumbnailPath = path.join(THUMBNAILS_DIR, thumbnailFilename);
  const thumbnailWebPath = `/thumbnails/${thumbnailFilename}`;
  const videoFullPath = path.join(VIDEOS_DIR, videoFilename);

  try {
    const { duration } = await extractThumbnailAndDuration(videoFullPath, thumbnailPath);

    db.run(
      `INSERT INTO videos (title, filename, video_path, thumbnail_path, user_id, user_name, size, duration) 
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        req.file.originalname.replace(/\.[^/.]+$/, ""),
        videoFilename,
        videoPath,
        thumbnailWebPath,
        userId,
        userName,
        req.file.size,
        duration
      ],
      function(err) {
        if (err) {
          return res.status(500).json({ 
            success: false, 
            error: 'Error guardando video: ' + err.message 
          });
        }

        const videoId = this.lastID;

        db.get(
          `SELECT * FROM videos WHERE id = ?`,
          [videoId],
          (err, newVideo) => {
            if (err) {
              return res.status(500).json({ 
                success: false, 
                error: 'Error obteniendo video: ' + err.message 
              });
            }

            // Notificar a todos los usuarios vía WebSocket
            const wsMessage = {
              type: 'VIDEO_UPLOADED',
              video: newVideo
            };
            broadcastToAll(wsMessage);

            res.status(200).json({ 
              success: true, 
              message: 'Video subido correctamente',
              videoId: videoId,
              video: newVideo
            });
          }
        );
      }
    );
  } catch (error) {
    console.error('Error procesando video:', error);

    // Subir sin miniatura
    db.run(
      `INSERT INTO videos (title, filename, video_path, user_id, user_name, size) 
       VALUES (?, ?, ?, ?, ?, ?)`,
      [
        req.file.originalname.replace(/\.[^/.]+$/, ""),
        videoFilename,
        videoPath,
        userId,
        userName,
        req.file.size
      ],
      function(err) {
        if (err) {
          return res.status(500).json({ 
            success: false, 
            error: 'Error guardando video: ' + err.message 
          });
        }

        res.status(200).json({ 
          success: true, 
          message: 'Video subido (sin miniatura)',
          videoId: this.lastID
        });
      }
    );
  }
});

// 2. OBTENER TODOS LOS VIDEOS
app.get('/api/videos', (req, res) => {
  const { limit, offset } = req.query;

  let query = 'SELECT * FROM videos WHERE 1=1';
  const params = [];

  query += ' ORDER BY created_at DESC';

  if (limit) {
    query += ' LIMIT ?';
    params.push(parseInt(limit));
  }

  if (offset) {
    query += ' OFFSET ?';
    params.push(parseInt(offset));
  }

  db.all(query, params, (err, videos) => {
    if (err) {
      return res.status(500).json({ error: err.message });
    }

    // Construir URLs completas
    const videosWithUrls = videos.map(video => ({
      ...video,
      video_url: buildFullUrl(video.video_path),
      thumbnail_url: video.thumbnail_path ? buildFullUrl(video.thumbnail_path) : null
    }));

    res.json({ 
      success: true, 
      count: videosWithUrls.length,
      videos: videosWithUrls 
    });
  });
});

// 3. OBTENER VÍDEO POR ID
app.get('/api/videos/:id', (req, res) => {
  const { id } = req.params;

  db.get('SELECT * FROM videos WHERE id = ?', [id], (err, video) => {
    if (err) return res.status(500).json({ error: err.message });
    if (!video) return res.status(404).json({ error: 'Video no encontrado' });

    video.video_url = buildFullUrl(video.video_path);
    video.thumbnail_url = video.thumbnail_path ? buildFullUrl(video.thumbnail_path) : null;

    res.json({ 
      success: true, 
      video: video 
    });
  });
});

// 4. INCREMENTAR VISTAS
app.post('/api/videos/:videoId/view', (req, res) => {
  const { videoId } = req.params;

  db.run(
    'UPDATE videos SET views = views + 1 WHERE id = ?',
    [videoId],
    function(err) {
      if (err) return res.status(500).json({ error: err.message });

      db.get('SELECT views FROM videos WHERE id = ?', [videoId], (err, video) => {
        if (!err && video) {
          // Notificar actualización vía WebSocket
          const wsMessage = {
            type: 'VIDEO_VIEW_UPDATE',
            videoId: parseInt(videoId),
            views: video.views
          };
          broadcastToAll(wsMessage);
        }
      });

      res.json({ success: true });
    }
  );
});

// 5. ELIMINAR VÍDEO
app.delete('/api/videos/:videoId', authenticateToken, (req, res) => {
  const { videoId } = req.params;
  const userId = req.user.id;

  db.get('SELECT user_id, filename, thumbnail_path FROM videos WHERE id = ?', [videoId], (err, video) => {
    if (err) return res.status(500).json({ error: err.message });
    if (!video) return res.status(404).json({ error: 'Video no encontrado' });
    if (video.user_id != userId) return res.status(403).json({ error: 'No tienes permiso' });

    // Eliminar archivos
    if (video.filename) {
      const videoPath = path.join(VIDEOS_DIR, video.filename);
      fs.unlink(videoPath, () => {});
    }

    if (video.thumbnail_path) {
      const thumbnailPath = path.join(THUMBNAILS_DIR, path.basename(video.thumbnail_path));
      fs.unlink(thumbnailPath, () => {});
    }

    db.run('DELETE FROM videos WHERE id = ?', [videoId], function(err) {
      if (err) return res.status(500).json({ error: err.message });

      // Notificar eliminación vía WebSocket
      const wsMessage = {
        type: 'VIDEO_DELETED',
        videoId: parseInt(videoId),
        userId: userId
      };
      broadcastToAll(wsMessage);

      res.json({ 
        success: true, 
        message: 'Video eliminado correctamente' 
      });
    });
  });
});

// ============================================
// RUTAS DE CHAT
// ============================================

// Guardar mensaje de chat
function saveChatMessage(message) {
  db.run(
    `INSERT INTO chat_messages (user_id, username, content, media_type, media_url) 
     VALUES (?, ?, ?, ?, ?)`,
    [
      message.user_id,
      message.username,
      message.content,
      message.media_type,
      message.media_url
    ],
    function(err) {
      if (err) {
        console.error('Error guardando mensaje de chat:', err);
      }
    }
  );
}

// Obtener mensajes recientes
function getRecentMessages(ws) {
  db.all(
    `SELECT * FROM chat_messages 
     ORDER BY created_at DESC 
     LIMIT 100`,
    [],
    (err, messages) => {
      if (!err && messages) {
        const recentMessages = messages.reverse(); // Ordenar del más antiguo al más nuevo
        ws.send(JSON.stringify({
          type: 'RECENT_MESSAGES',
          messages: recentMessages
        }));
      }
    }
  );
}

// Subir archivo para chat
app.post('/api/chat/upload-media', authenticateToken, uploadChatMedia.single('media'), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ error: 'No se subió ningún archivo' });
  }

  const mediaType = req.file.mimetype.startsWith('image/') ? 'image' : 'video';
  const mediaUrl = `/uploads/chat_media/${req.file.filename}`;

  res.json({
    success: true,
    media_type: mediaType,
    media_url: buildFullUrl(mediaUrl)
  });
});

// ============================================
// RUTAS ADICIONALES
// ============================================

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/api/health', (req, res) => {
  res.json({
    status: 'ok',
    timestamp: new Date().toISOString(),
    environment: isProduction ? 'production' : 'development',
    service: 'CPVideos API',
    version: '2.0.0',
    features: ['JWT Auth', 'WebSockets', 'Real-time Chat', 'Video Upload'],
    onlineUsers: onlineUsers.size,
    activeConnections: activeConnections.size
  });
});

// ============================================
// MANEJO DE ERRORES
// ============================================

app.use((err, req, res, next) => {
  if (err instanceof multer.MulterError) {
    if (err.code === 'LIMIT_FILE_SIZE') {
      return res.status(413).json({ error: 'El archivo es demasiado grande' });
    }
    return res.status(400).json({ error: `Error subiendo archivo: ${err.message}` });
  }

  if (err) {
    console.error('🔥 Error:', err.message);
    return res.status(500).json({ 
      error: 'Error interno del servidor',
      message: err.message 
    });
  }

  next();
});

app.use((req, res) => {
  res.status(404).json({ error: 'Ruta no encontrada' });
});

// ============================================
// INICIAR SERVIDOR
// ============================================

server.listen(PORT, () => {
  console.log(`
╔═══════════════════════════════════════════════╗
║     📺 CPVideos Server v2.0.0 📺             ║
╠═══════════════════════════════════════════════╣
║  Puerto:        ${PORT}                       
║  Entorno:       ${isProduction ? 'production' : 'development'}                   
║  Versión:       2.0.0 (Chat en Tiempo Real)           
║  WebSocket:     ws://localhost:${PORT}/ws     
║  Upload Dir:    ${VIDEOS_DIR}                
║  Chat Media:    ${CHAT_MEDIA_DIR}            
║  URL:           http://localhost:${PORT}      
╚═══════════════════════════════════════════════╝
      
✅ Servidor HTTP corriendo en http://localhost:${PORT}
🔌 WebSocket Server activo en ws://localhost:${PORT}/ws
🔐 Autenticación: JWT habilitada
💬 Chat: Comunicación en tiempo real
📤 Upload: Subida de videos
⏱️  Duración: Extracción automática
🔧 Health Check: http://localhost:${PORT}/api/health
💻 Sistema de chat FULL ACTIVO
  `);
});