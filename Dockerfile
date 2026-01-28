FROM python:3.9-slim

# Instalar solo dependencias esenciales del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar requirements primero (para mejor caché de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de archivos
COPY . .

# Crear directorios necesarios
RUN mkdir -p temp_videos converted_videos

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "server:app"]