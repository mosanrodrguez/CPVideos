package main

import (
    "encoding/json"
    "fmt"
    "io"
    "log"
    "net/http"
    "os"
    "os/exec"
    "os/signal"
    "path/filepath"
    "strconv"
    "strings"
    "syscall"
    "time"

    tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
)

type DownloadBot struct {
    bot         *tgbotapi.BotAPI
    downloadDir string
    userStates  map[int64]*UserState
}

type UserState struct {
    LastURL    string
    LastFormat string // "video" o "audio"
    Formats    []FormatInfo
}

type FormatInfo struct {
    FormatID   string `json:"format_id"`
    Ext        string `json:"ext"`
    Resolution string `json:"resolution"`
    Filesize   int64  `json:"filesize,omitempty"`
    FormatNote string `json:"format_note"`
    AudioCodec string `json:"acodec"`
    VideoCodec string `json:"vcodec"`
}

type VideoInfo struct {
    Title    string       `json:"title"`
    Formats  []FormatInfo `json:"formats"`
    WebpageURL string     `json:"webpage_url"`
}

func main() {
    // Obtener token
    token := os.Getenv("TELEGRAM_BOT_TOKEN")
    if token == "" {
        log.Fatal("❌ Configura TELEGRAM_BOT_TOKEN en variables de entorno")
    }

    // Crear bot
    bot, err := tgbotapi.NewBotAPI(token)
    if err != nil {
        log.Panic("❌ Error creando bot:", err)
    }

    bot.Debug = true
    log.Printf("🤖 Bot iniciado como: @%s", bot.Self.UserName)

    // Crear directorio de descargas
    downloadDir := "./temp_downloads"
    if err := os.MkdirAll(downloadDir, 0755); err != nil {
        log.Fatal("❌ Error creando directorio:", err)
    }

    // Crear instancia del bot
    downloadBot := &DownloadBot{
        bot:         bot,
        downloadDir: downloadDir,
        userStates:  make(map[int64]*UserState),
    }

    // Limpiador automático
    go downloadBot.autoCleaner()

    // Configurar updates
    u := tgbotapi.NewUpdate(0)
    u.Timeout = 60
    updates := bot.GetUpdatesChan(u)

    // Manejar señales de cierre
    sigChan := make(chan os.Signal, 1)
    signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

    log.Println("✅ Bot listo. Envía un enlace para comenzar...")

    // Procesar mensajes
    for {
        select {
        case update := <-updates:
            downloadBot.handleUpdate(update)
        case <-sigChan:
            log.Println("🔄 Apagando bot...")
            // Limpiar archivos temporales
            os.RemoveAll(downloadDir)
            return
        }
    }
}

func (b *DownloadBot) handleUpdate(update tgbotapi.Update) {
    if update.Message != nil {
        b.handleMessage(update.Message)
    } else if update.CallbackQuery != nil {
        b.handleCallback(update.CallbackQuery)
    }
}

func (b *DownloadBot) handleMessage(message *tgbotapi.Message) {
    chatID := message.Chat.ID
    text := strings.TrimSpace(message.Text)

    // Verificar si es un enlace
    if strings.HasPrefix(text, "http://") || strings.HasPrefix(text, "https://") {
        b.processLink(chatID, text)
        return
    }

    // Comandos
    if message.IsCommand() {
        switch message.Command() {
        case "start":
            b.sendMessage(chatID, 
                "🎬 *Bienvenido a VideoDown*\n\n" +
                "📥 *¿Cómo usar?*\n" +
                "1. Envía cualquier enlace de video\n" +
                "2. Selecciona si quieres Video o Audio\n" +
                "3. Elige la calidad deseada\n" +
                "4. ¡Listo! El archivo se descargará y enviará automáticamente\n\n" +
                "⚠️ *Nota:* Este bot se encuentra en desarrollo, puede reportar errores y sugerencias a @mosanrodrguez.")
        case "help":
            b.sendMessage(chatID,
                "🆘 *Ayuda*\n\n" +
                "• Solo envía un enlace y sigue los pasos\n" +
                "• Formatos soportados: MP4, MP3, M4A, WEBM\n" +
                "• Plataformas: YouTube, TikTok, Instagram, Twitter, Facebook, etc.\n" +
                "• Bot en desarrollo, pueden ocurrir fallos.")
        default:
            b.sendMessage(chatID, "❓ Comando no reconocido. Envía un enlace para comenzar.")
        }
        return
    }

    b.sendMessage(chatID, "📥 Envía un enlace de video para descargarlo.")
}

func (b *DownloadBot) processLink(chatID int64, url string) {
    // Mostrar mensaje de procesamiento
    msg := b.sendMessage(chatID, "🔍 Verificando enlace...")

    // Verificar si el enlace es válido
    if !b.isValidURL(url) {
        b.editMessage(chatID, msg.MessageID, "❌ Enlace no válido o no soportado")
        return
    }

    // Guardar estado del usuario
    b.userStates[chatID] = &UserState{
        LastURL: url,
    }

    // Mostrar opciones Video/Audio
    b.editMessage(chatID, msg.MessageID, "✅ Enlace válido\n\n¿Qué deseas descargar?")
    b.sendFormatOptions(chatID)
}

func (b *DownloadBot) sendFormatOptions(chatID int64) {
    keyboard := tgbotapi.NewInlineKeyboardMarkup(
        tgbotapi.NewInlineKeyboardRow(
            tgbotapi.NewInlineKeyboardButtonData("🎥 Video", "type:video"),
            tgbotapi.NewInlineKeyboardButtonData("🎵 Audio", "type:audio"),
        ),
    )

    msg := tgbotapi.NewMessage(chatID, "Selecciona el tipo de descarga:")
    msg.ReplyMarkup = keyboard
    b.bot.Send(msg)
}

func (b *DownloadBot) handleCallback(callback *tgbotapi.CallbackQuery) {
    chatID := callback.Message.Chat.ID
    data := callback.Data
    messageID := callback.Message.MessageID

    // Responder al callback
    b.bot.Send(tgbotapi.NewCallback(callback.ID, "⏳ Procesando..."))

    parts := strings.Split(data, ":")
    if len(parts) < 2 {
        return
    }

    action := parts[0]
    value := parts[1]

    state, exists := b.userStates[chatID]
    if !exists || state.LastURL == "" {
        b.editMessage(chatID, messageID, "❌ Sesión expirada. Envía el enlace nuevamente.")
        return
    }

    switch action {
    case "type":
        // Video o Audio seleccionado
        state.LastFormat = value
        b.editMessage(chatID, messageID, fmt.Sprintf("🔍 Buscando formatos de %s disponibles...", value))
        b.showAvailableFormats(chatID, state.LastURL, value)

    case "format":
        // Formato específico seleccionado
        if len(parts) == 3 {
            formatID := parts[2]
            b.downloadAndSend(chatID, state.LastURL, formatID, value)
            // Eliminar mensaje con botones
            b.deleteMessage(chatID, messageID)
        }

    case "cancel":
        b.deleteMessage(chatID, messageID)
        delete(b.userStates, chatID)
    }
}

func (b *DownloadBot) showAvailableFormats(chatID int64, url, formatType string) {
    // Obtener información del video
    info, err := b.getVideoInfo(url)
    if err != nil {
        b.sendMessage(chatID, "❌ Error al obtener información del video")
        return
    }

    // Filtrar formatos según el tipo seleccionado
    var availableFormats []FormatInfo
    if formatType == "video" {
        availableFormats = b.filterVideoFormats(info.Formats)
    } else {
        availableFormats = b.filterAudioFormats(info.Formats)
    }

    if len(availableFormats) == 0 {
        b.sendMessage(chatID, "❌ No se encontraron formatos disponibles")
        return
    }

    // Guardar formatos en el estado
    if state, exists := b.userStates[chatID]; exists {
        state.Formats = availableFormats
    }

    // Mostrar botones con formatos disponibles
    b.sendFormatButtons(chatID, availableFormats, formatType, info.Title)
}

func (b *DownloadBot) sendFormatButtons(chatID int64, formats []FormatInfo, formatType, title string) {
    // Limitar a 8 formatos para no saturar
    if len(formats) > 8 {
        formats = formats[:8]
    }

    // Crear filas de botones
    var rows [][]tgbotapi.InlineKeyboardButton
    for i, format := range formats {
        // Crear etiqueta para el botón
        label := b.formatLabel(format, formatType)
        
        // Crear callback data: format:type:formatID
        callbackData := fmt.Sprintf("format:%s:%s", formatType, format.FormatID)
        
        btn := tgbotapi.NewInlineKeyboardButtonData(label, callbackData)
        
        // Agregar a filas (2 botones por fila)
        if i%2 == 0 {
            rows = append(rows, []tgbotapi.InlineKeyboardButton{btn})
        } else {
            rows[len(rows)-1] = append(rows[len(rows)-1], btn)
        }
    }

    // Agregar botón de cancelar
    rows = append(rows, []tgbotapi.InlineKeyboardButton{
        tgbotapi.NewInlineKeyboardButtonData("❌ Cancelar", "cancel"),
    })

    keyboard := tgbotapi.NewInlineKeyboardMarkup(rows...)

    // Crear mensaje con título truncado
    displayTitle := title
    if len(displayTitle) > 50 {
        displayTitle = displayTitle[:47] + "..."
    }

    msgText := fmt.Sprintf("📋 *%s*\n\n", displayTitle)
    msgText += fmt.Sprintf("Selecciona una calidad de *%s*:\n", formatType)
    
    msg := tgbotapi.NewMessage(chatID, msgText)
    msg.ParseMode = "Markdown"
    msg.ReplyMarkup = keyboard
    b.bot.Send(msg)
}

func (b *DownloadBot) formatLabel(format FormatInfo, formatType string) string {
    var label string
    
    if formatType == "video" {
        // Para video: Resolución + Formato + Tamaño
        if format.Resolution != "" {
            label = format.Resolution
        } else if format.FormatNote != "" {
            label = format.FormatNote
        } else {
            label = "SD"
        }
        
        label += " " + strings.ToUpper(format.Ext)
        
    } else {
        // Para audio: Formato + Calidad + Tamaño
        label = strings.ToUpper(format.Ext)
        if format.FormatNote != "" {
            label += " " + format.FormatNote
        }
    }
    
    // Agregar tamaño si está disponible
    if format.Filesize > 0 {
        sizeMB := float64(format.Filesize) / (1024 * 1024)
        label += fmt.Sprintf(" (%.1fMB)", sizeMB)
    }
    
    return label
}

func (b *DownloadBot) downloadAndSend(chatID int64, url, formatID, formatType string) {
    // Notificar inicio de descarga
    statusMsg := b.sendMessage(chatID, "⏬ Descargando...")

    // Crear nombre de archivo único
    filename := fmt.Sprintf("%d_%s_%d", chatID, formatID, time.Now().Unix())
    outputPath := filepath.Join(b.downloadDir, filename+".%(ext)s")

    // Preparar comando yt-dlp
    var cmd *exec.Cmd
    if formatType == "audio" {
        cmd = exec.Command("yt-dlp",
            "-f", formatID,
            "-x", // Extraer audio
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", outputPath,
            "--no-playlist",
            url,
        )
    } else {
        cmd = exec.Command("yt-dlp",
            "-f", formatID,
            "-o", outputPath,
            "--no-playlist",
            url,
        )
    }

    // Ejecutar descarga
    if err := cmd.Run(); err != nil {
        b.editMessage(chatID, statusMsg.MessageID, "❌ Error al descargar")
        return
    }

    // Buscar archivo descargado
    downloadedFile, err := b.findDownloadedFile(chatID, formatID)
    if err != nil {
        b.editMessage(chatID, statusMsg.MessageID, "❌ Archivo no encontrado")
        return
    }

    // Obtener información del archivo
    fileInfo, _ := os.Stat(downloadedFile)
    fileSize := fileInfo.Size()

    // Verificar límite de Telegram (50MB)
    if fileSize > 50*1024*1024 {
        b.editMessage(chatID, statusMsg.MessageID, "❌ Archivo muy grande (límite: 50MB)")
        os.Remove(downloadedFile)
        return
    }

    // Enviar archivo
    b.editMessage(chatID, statusMsg.MessageID, "📤 Enviando...")
    b.sendFileToUser(chatID, downloadedFile, formatType)

    // Limpiar
    b.deleteMessage(chatID, statusMsg.MessageID)
    os.Remove(downloadedFile)
    delete(b.userStates, chatID)
}

func (b *DownloadBot) sendFileToUser(chatID int64, filePath, formatType string) {
    file, err := os.Open(filePath)
    if err != nil {
        b.sendMessage(chatID, "❌ Error al abrir archivo")
        return
    }
    defer file.Close()

    // Extraer nombre del archivo
    filename := filepath.Base(filePath)

    if formatType == "audio" {
        // Enviar como audio
        audio := tgbotapi.NewAudio(chatID, tgbotapi.FilePath(filePath))
        audio.Title = strings.TrimSuffix(filename, filepath.Ext(filename))
        _, err = b.bot.Send(audio)
    } else {
        // Enviar como video
        video := tgbotapi.NewVideo(chatID, tgbotapi.FilePath(filePath))
        _, err = b.bot.Send(video)
    }

    if err != nil {
        log.Printf("Error enviando archivo: %v", err)
        b.sendMessage(chatID, "❌ Error al enviar archivo")
    }
}

func (b *DownloadBot) findDownloadedFile(chatID int64, formatID string) (string, error) {
    pattern := filepath.Join(b.downloadDir, fmt.Sprintf("%d_%s_*", chatID, formatID))
    files, err := filepath.Glob(pattern)
    if err != nil || len(files) == 0 {
        return "", fmt.Errorf("archivo no encontrado")
    }
    
    // Buscar el más reciente
    var latestFile string
    var latestTime time.Time
    
    for _, file := range files {
        info, err := os.Stat(file)
        if err != nil {
            continue
        }
        if info.ModTime().After(latestTime) {
            latestTime = info.ModTime()
            latestFile = file
        }
    }
    
    if latestFile == "" {
        return "", fmt.Errorf("archivo no encontrado")
    }
    
    return latestFile, nil
}

func (b *DownloadBot) getVideoInfo(url string) (*VideoInfo, error) {
    cmd := exec.Command("yt-dlp", "-j", "--no-playlist", url)
    output, err := cmd.Output()
    if err != nil {
        return nil, err
    }

    var info VideoInfo
    if err := json.Unmarshal(output, &info); err != nil {
        return nil, err
    }

    return &info, nil
}

func (b *DownloadBot) filterVideoFormats(formats []FormatInfo) []FormatInfo {
    var videoFormats []FormatInfo
    
    for _, format := range formats {
        // Filtrar formatos que tienen video y audio
        if format.VideoCodec != "none" && format.AudioCodec != "none" {
            // Priorizar MP4 y WEBM
            if format.Ext == "mp4" || format.Ext == "webm" {
                videoFormats = append(videoFormats, format)
            }
        }
    }
    
    return videoFormats
}

func (b *DownloadBot) filterAudioFormats(formats []FormatInfo) []FormatInfo {
    var audioFormats []FormatInfo
    
    for _, format := range formats {
        // Filtrar formatos que solo tienen audio
        if format.VideoCodec == "none" && format.AudioCodec != "none" {
            audioFormats = append(audioFormats, format)
        }
    }
    
    return audioFormats
}

func (b *DownloadBot) isValidURL(url string) bool {
    // Verificación simple
    if !strings.HasPrefix(url, "http") {
        return false
    }
    
    // Verificar con yt-dlp si es soportado
    cmd := exec.Command("yt-dlp", "--dump-json", "--no-playlist", url)
    return cmd.Run() == nil
}

func (b *DownloadBot) autoCleaner() {
    ticker := time.NewTicker(5 * time.Minute)
    defer ticker.Stop()

    for range ticker.C {
        files, _ := filepath.Glob(filepath.Join(b.downloadDir, "*"))
        for _, file := range files {
            info, err := os.Stat(file)
            if err != nil {
                continue
            }
            
            // Eliminar archivos con más de 1 hora
            if time.Since(info.ModTime()) > time.Hour {
                os.Remove(file)
                log.Printf("🧹 Limpiado: %s", file)
            }
        }
    }
}

// Métodos auxiliares para mensajes
func (b *DownloadBot) sendMessage(chatID int64, text string) tgbotapi.Message {
    msg := tgbotapi.NewMessage(chatID, text)
    sentMsg, _ := b.bot.Send(msg)
    return sentMsg
}

func (b *DownloadBot) editMessage(chatID int64, messageID int, text string) {
    editMsg := tgbotapi.NewEditMessageText(chatID, messageID, text)
    b.bot.Send(editMsg)
}

func (b *DownloadBot) deleteMessage(chatID int64, messageID int) {
    delMsg := tgbotapi.NewDeleteMessage(chatID, messageID)
    b.bot.Send(delMsg)
}