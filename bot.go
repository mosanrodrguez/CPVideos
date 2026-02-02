package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
)

// Configuración global
const (
	MaxFileSizeBotAPI = 50 * 1024 * 1024 // 50MB (Límite estándar de Telegram Bot API)
	DownloadDir       = "./temp_downloads"
	UpdateInterval    = 3 * time.Second // Intervalo para actualizar la barra de progreso
	
	// Token del bot - CAMBIA ESTO CON TU TOKEN REAL
	BotToken = "8260660352:AAFPSK2-GXqGoBm2b3K988B_dadPXHduc5M"
	
	// URL del webhook - CAMBIA ESTO CON TU URL DE RENDER
	WebhookURL = "https://tgbotvd.onrender.com/webhook"
	
	// Puerto para el servidor HTTP
	Port = "8080"
)

type DownloadBot struct {
	bot        *tgbotapi.BotAPI
	httpClient *http.Client
	userStates sync.Map // Thread-safe map
}

type VideoMetaData struct {
	ID         string  `json:"id"`
	Title      string  `json:"title"`
	Duration   float64 `json:"duration"`
	Thumbnail  string  `json:"thumbnail"`
	WebpageURL string  `json:"webpage_url"`
	Formats    []struct {
		FormatID   string `json:"format_id"`
		Ext        string `json:"ext"`
		Height     int    `json:"height"`
		VideoCodec string `json:"vcodec"`
		AudioCodec string `json:"acodec"`
		Filesize   int64  `json:"filesize,omitempty"`
	} `json:"formats"`
}

func main() {
	// Verificar herramientas externas
	if _, err := exec.LookPath("yt-dlp"); err != nil {
		log.Fatal("❌ 'yt-dlp' no está instalado o no está en el PATH.")
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		log.Fatal("❌ 'ffmpeg' no está instalado. Es necesario para procesar videos.")
	}

	// Crear instancia del bot
	bot, err := tgbotapi.NewBotAPI(BotToken)
	if err != nil {
		log.Panic("❌ Error creando bot:", err)
	}

	bot.Debug = false
	log.Printf("🤖 Bot iniciado como: @%s", bot.Self.UserName)

	// Configurar webhook
	log.Println("🌐 Configurando webhook...")
	_, err = bot.Request(tgbotapi.NewWebhook(WebhookURL))
	if err != nil {
		log.Fatal("❌ Error configurando webhook:", err)
	}

	// Crear directorio temporal
	if err := os.MkdirAll(DownloadDir, 0755); err != nil {
		log.Fatal("❌ Error creando directorio:", err)
	}

	// Crear instancia del bot de descarga
	downloadBot := &DownloadBot{
		bot:        bot,
		httpClient: &http.Client{Timeout: 30 * time.Second},
	}

	// Limpiador automático en segundo plano
	go downloadBot.autoCleaner()

	// Manejo graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigChan
		log.Println("🔄 Apagando bot y limpiando...")
		os.RemoveAll(DownloadDir)
		os.Exit(0)
	}()

	// Configurar endpoints HTTP
	http.HandleFunc("/webhook", downloadBot.webhookHandler)
	http.HandleFunc("/health", downloadBot.healthHandler)
	
	// Info endpoint
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"status": "online",
			"bot":    bot.Self.UserName,
			"time":   time.Now().Format(time.RFC3339),
		})
	})

	log.Printf("🚀 Servidor iniciado en puerto %s", Port)
	log.Printf("📞 Webhook configurado en: %s", WebhookURL)
	log.Fatal(http.ListenAndServe(":"+Port, nil))
}

// Handler para webhook de Telegram
func (b *DownloadBot) webhookHandler(w http.ResponseWriter, r *http.Request) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("⚠️ Pánico en webhook: %v", r)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}()

	update, err := b.bot.HandleUpdate(r)
	if err != nil {
		log.Printf("❌ Error procesando update: %v", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// Procesar update en goroutine para no bloquear
	go b.handleUpdate(*update)
	
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("OK"))
}

// Handler para health checks de Render
func (b *DownloadBot) healthHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("OK"))
}

func (b *DownloadBot) handleUpdate(update tgbotapi.Update) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("⚠️ Pánico recuperado: %v", r)
		}
	}()

	if update.Message != nil {
		b.handleMessage(update.Message)
	} else if update.CallbackQuery != nil {
		b.handleCallback(update.CallbackQuery)
	}
}

func (b *DownloadBot) handleMessage(message *tgbotapi.Message) {
	chatID := message.Chat.ID
	text := strings.TrimSpace(message.Text)

	if message.IsCommand() {
		switch message.Command() {
		case "start", "help":
			b.sendMessage(chatID, "🎬 *Video Downloader Pro*\n\nEnvía un enlace de YouTube, TikTok, Instagram, Twitter, etc.\n\nEl bot detectará automáticamente las calidades disponibles.")
		case "status":
			b.sendMessage(chatID, "✅ Bot funcionando correctamente\n\nEnvía un enlace para descargar contenido.")
		}
		return
	}

	if strings.HasPrefix(text, "http") {
		b.processLink(chatID, text)
	} else {
		b.sendMessage(chatID, "📥 Por favor, envía un enlace válido (YouTube, TikTok, Instagram, etc.).")
	}
}

func (b *DownloadBot) processLink(chatID int64, url string) {
	msg := b.sendMessage(chatID, "🔍 *Analizando enlace...*")

	// Usamos contexto para cancelar si tarda mucho
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "yt-dlp", "-j", "--no-playlist", url)
	output, err := cmd.Output()

	if err != nil {
		log.Printf("Error yt-dlp: %v", err)
		b.editMessage(chatID, msg.MessageID, "❌ No se pudo procesar el enlace. Verifica que sea público y válido.")
		return
	}

	var meta VideoMetaData
	if err := json.Unmarshal(output, &meta); err != nil {
		b.editMessage(chatID, msg.MessageID, "❌ Error leyendo metadatos.")
		return
	}

	// Guardamos estado temporalmente
	b.userStates.Store(chatID, &meta)

	// Crear teclado
	keyboard := b.createQualityKeyboard(chatID, &meta)
	b.editMessageMarkup(chatID, msg.MessageID, fmt.Sprintf("🎥 *%s*\n\nSelecciona una opción:", escapeMarkdown(meta.Title)), keyboard)
}

func (b *DownloadBot) createQualityKeyboard(chatID int64, meta *VideoMetaData) tgbotapi.InlineKeyboardMarkup {
	var rows [][]tgbotapi.InlineKeyboardButton

	// 1. Botón Audio MP3
	rows = append(rows, []tgbotapi.InlineKeyboardButton{
		tgbotapi.NewInlineKeyboardButtonData("🎵 Audio (MP3)", "dl:audio:best"),
	})

	// 2. Analizar resoluciones de video únicas
	resolutions := make(map[int]bool)
	for _, f := range meta.Formats {
		// Solo queremos formatos de video que tengan una altura definida
		if f.VideoCodec != "none" && f.Height > 0 {
			resolutions[f.Height] = true
		}
	}

	// Convertir map a slice para ordenar
	var heights []int
	for h := range resolutions {
		heights = append(heights, h)
	}
	sort.Sort(sort.Reverse(sort.IntSlice(heights))) // De mayor a menor

	// Crear botones para resoluciones (máximo 4 para no saturar)
	var videoRow []tgbotapi.InlineKeyboardButton
	count := 0
	for _, h := range heights {
		if count >= 4 {
			break
		}
		label := fmt.Sprintf("%dp", h)
		data := fmt.Sprintf("dl:video:%d", h)
		videoRow = append(videoRow, tgbotapi.NewInlineKeyboardButtonData(label, data))
		count++
	}

	// Dividir botones de video en filas de 2
	for i := 0; i < len(videoRow); i += 2 {
		end := i + 2
		if end > len(videoRow) {
			end = len(videoRow)
		}
		rows = append(rows, videoRow[i:end])
	}

	rows = append(rows, []tgbotapi.InlineKeyboardButton{
		tgbotapi.NewInlineKeyboardButtonData("❌ Cancelar", "cancel"),
	})

	return tgbotapi.NewInlineKeyboardMarkup(rows...)
}

func (b *DownloadBot) handleCallback(cb *tgbotapi.CallbackQuery) {
	data := cb.Data
	chatID := cb.Message.Chat.ID
	msgID := cb.Message.MessageID

	// Respuesta rápida para que el relojito de carga desaparezca
	b.bot.Request(tgbotapi.NewCallback(cb.ID, ""))

	if data == "cancel" {
		b.deleteMessage(chatID, msgID)
		b.userStates.Delete(chatID)
		return
	}

	parts := strings.Split(data, ":")
	if len(parts) < 3 || parts[0] != "dl" {
		return
	}

	mode := parts[1] // video o audio
	quality := parts[2]

	val, ok := b.userStates.Load(chatID)
	if !ok {
		b.editMessage(chatID, msgID, "❌ Sesión expirada. Envía el enlace de nuevo.")
		return
	}
	meta := val.(*VideoMetaData)

	// Iniciar proceso de descarga en goroutine
	go b.performDownload(chatID, msgID, meta, mode, quality)
}

func (b *DownloadBot) performDownload(chatID int64, msgID int, meta *VideoMetaData, mode, quality string) {
	// 1. Preparar rutas
	fileName := fmt.Sprintf("vid_%d_%d", chatID, time.Now().Unix())
	filePathNoExt := filepath.Join(DownloadDir, fileName)
	
	// Plantilla de salida para yt-dlp
	outputTemplate := filePathNoExt + ".%(ext)s"

	var args []string
	var finalExt string

	// 2. Configurar argumentos de yt-dlp
	if mode == "audio" {
		finalExt = ".mp3"
		args = []string{
			"-f", "bestaudio/best",
			"-x", "--audio-format", "mp3",
			"--audio-quality", "0",
			"-o", outputTemplate,
			meta.WebpageURL,
		}
	} else {
		// Video: Usar fusión de streams si es necesario
		finalExt = ".mp4"
		formatSelector := fmt.Sprintf("bestvideo[height<=%s]+bestaudio/best[height<=%s]/best", quality, quality)
		
		args = []string{
			"-f", formatSelector,
			"--merge-output-format", "mp4",
			"-o", outputTemplate,
			meta.WebpageURL,
		}
	}

	// 3. Ejecutar descarga con monitoreo de progreso
	b.editMessage(chatID, msgID, "🚀 *Iniciando descarga...*")
	
	finalPath := filePathNoExt + finalExt
	
	// Usamos un cmd wrapper para leer stdout
	cmd := exec.Command("yt-dlp", args...)
	
	// Pipe para leer el progreso
	stdout, _ := cmd.StdoutPipe()
	if err := cmd.Start(); err != nil {
		b.editMessage(chatID, msgID, "❌ Error al iniciar descarga.")
		return
	}

	// Monitor de progreso
	done := make(chan bool)
	go b.monitorProgress(stdout, chatID, msgID, done)

	err := cmd.Wait()
	done <- true // Detener monitor

	if err != nil {
		log.Printf("Error descarga: %v", err)
		b.editMessage(chatID, msgID, "❌ Error durante la descarga o conversión.")
		os.Remove(finalPath) // Limpieza
		return
	}

	// 4. Verificación de archivo
	fileInfo, err := os.Stat(finalPath)
	if err != nil {
		b.editMessage(chatID, msgID, "❌ Archivo no encontrado tras descarga.")
		return
	}

	if fileInfo.Size() > MaxFileSizeBotAPI {
		b.editMessage(chatID, msgID, fmt.Sprintf("❌ El archivo es demasiado grande (%d MB). El límite de Telegram es 50MB.", fileInfo.Size()/(1024*1024)))
		os.Remove(finalPath)
		return
	}

	// 5. Descargar miniatura (Thumbnail)
	thumbPath := ""
	if meta.Thumbnail != "" {
		thumbPath = filepath.Join(DownloadDir, fileName+"_thumb.jpg")
		if err := b.downloadFile(meta.Thumbnail, thumbPath); err != nil {
			thumbPath = "" // Si falla, enviamos sin thumbnail
		}
	}

	// 6. Subir a Telegram
	b.editMessage(chatID, msgID, "📤 *Subiendo a Telegram...*")
	b.uploadFile(chatID, finalPath, thumbPath, mode, meta, msgID)
	
	// 7. Limpieza final
	os.Remove(finalPath)
	if thumbPath != "" {
		os.Remove(thumbPath)
	}
	b.userStates.Delete(chatID)
	b.deleteMessage(chatID, msgID) // Borrar mensaje de estado
}

func (b *DownloadBot) monitorProgress(r io.Reader, chatID int64, msgID int, done chan bool) {
	scanner := bufio.NewScanner(r)
	ticker := time.NewTicker(UpdateInterval)
	defer ticker.Stop()

	var lastLine string
	
	// Regex para capturar porcentaje de yt-dlp [download] 45.5% ...
	re := regexp.MustCompile(`\[download\]\s+(\d+\.\d+)%`)

	for {
		select {
		case <-done:
			return
		case <-ticker.C:
			if lastLine == "" {
				continue
			}
			matches := re.FindStringSubmatch(lastLine)
			if len(matches) > 1 {
				percent := matches[1]
				bar := generateProgressBar(percent)
				b.editMessage(chatID, msgID, fmt.Sprintf("⏬ *Descargando: %s%%*\n%s", percent, bar))
			}
		default:
			if scanner.Scan() {
				text := scanner.Text()
				// Solo guardamos líneas de descarga, ignoramos logs de ffmpeg
				if strings.Contains(text, "[download]") {
					lastLine = text
				}
			} else {
				// Si termina el scan, esperamos señal done
				time.Sleep(500 * time.Millisecond)
			}
		}
	}
}

func (b *DownloadBot) uploadFile(chatID int64, filePath, thumbPath, mode string, meta *VideoMetaData, statusMsgID int) {
	file := tgbotapi.FilePath(filePath)

	var msg tgbotapi.Chattable
	
	if mode == "audio" {
		audio := tgbotapi.NewAudio(chatID, file)
		audio.Title = meta.Title
		audio.Performer = "Bot Download"
		if thumbPath != "" {
			thumb := tgbotapi.FilePath(thumbPath)
			audio.Thumb = thumb
		}
		msg = audio
	} else {
		video := tgbotapi.NewVideo(chatID, file)
		video.Caption = fmt.Sprintf("🎬 %s", meta.Title)
		video.Duration = int(meta.Duration)
		
		// Determinar dimensiones aproximadas si es posible, o dejar que Telegram decida
		video.SupportsStreaming = true
		
		if thumbPath != "" {
			thumb := tgbotapi.FilePath(thumbPath)
			video.Thumb = thumb
		}
		msg = video
	}

	_, err := b.bot.Send(msg)
	if err != nil {
		log.Printf("Error enviando archivo: %v", err)
		b.sendMessage(chatID, "❌ Ocurrió un error enviando el archivo a Telegram.")
	}
}

// Utilidades

func (b *DownloadBot) downloadFile(url, filepath string) error {
	resp, err := b.httpClient.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	out, err := os.Create(filepath)
	if err != nil {
		return err
	}
	defer out.Close()

	_, err = io.Copy(out, resp.Body)
	return err
}

func generateProgressBar(percentStr string) string {
	p, _ := strconv.ParseFloat(percentStr, 64)
	totalBars := 10
	filledBars := int(math.Round((p / 100) * float64(totalBars)))
	
	bar := strings.Repeat("▓", filledBars) + strings.Repeat("░", totalBars-filledBars)
	return bar
}

func (b *DownloadBot) sendMessage(chatID int64, text string) tgbotapi.Message {
	msg := tgbotapi.NewMessage(chatID, text)
	msg.ParseMode = "Markdown"
	sent, _ := b.bot.Send(msg)
	return sent
}

func (b *DownloadBot) editMessage(chatID int64, msgID int, text string) {
	msg := tgbotapi.NewEditMessageText(chatID, msgID, text)
	msg.ParseMode = "Markdown"
	b.bot.Send(msg)
}

func (b *DownloadBot) editMessageMarkup(chatID int64, msgID int, text string, markup tgbotapi.InlineKeyboardMarkup) {
	msg := tgbotapi.NewEditMessageText(chatID, msgID, text)
	msg.ParseMode = "Markdown"
	msg.ReplyMarkup = &markup
	b.bot.Send(msg)
}

func (b *DownloadBot) deleteMessage(chatID int64, msgID int) {
	b.bot.Send(tgbotapi.NewDeleteMessage(chatID, msgID))
}

func (b *DownloadBot) autoCleaner() {
	ticker := time.NewTicker(10 * time.Minute)
	for range ticker.C {
		files, _ := filepath.Glob(filepath.Join(DownloadDir, "*"))
		for _, f := range files {
			info, err := os.Stat(f)
			if err == nil && time.Since(info.ModTime()) > 30*time.Minute {
				os.Remove(f)
			}
		}
	}
}

func escapeMarkdown(text string) string {
	// Simple escape para evitar errores básicos de markdown
	return strings.NewReplacer("_", "\\_", "*", "\\*", "[", "\\[", "`", "\\`").Replace(text)
}