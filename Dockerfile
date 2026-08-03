FROM python:3.14.4-slim

ENV PYTHONUNBUFFERED=1

# Only the minimal deps you actually need
RUN apt-get update && apt-get install -y \
    wget \
    xz-utils \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js via official Debian/Ubuntu setup (Cleaner & more reliable)
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Install 7z
RUN wget -q https://github.com/ip7z/7zip/releases/download/26.02/7z2602-linux-x64.tar.xz \
    -O /tmp/7z.tar.xz \
    && tar -xf /tmp/7z.tar.xz -C /tmp \
    && mv /tmp/7zz /usr/local/bin/7z \
    && chmod +x /usr/local/bin/7z \
    && rm -rf /tmp/7z.tar.xz \
    && 7z

# Install RAR (standalone binary style)
RUN wget -q https://www.rarlab.com/rar/rarlinux-x64-721b1.tar.gz \
    -O /tmp/rar.tar.gz \
    && tar -xzf /tmp/rar.tar.gz -C /tmp \
    && mv /tmp/rar/rar /usr/local/bin/rar \
    && chmod +x /usr/local/bin/rar \
    && rm -rf /tmp/rar /tmp/rar.tar.gz \
    && rar

# Install yt-dlp as standalone binary
RUN wget -q https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -O /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp \
    && yt-dlp --version

# Install tdl
RUN wget -q https://github.com/iyear/tdl/releases/latest/download/tdl_Linux_64bit.tar.gz \
    && tar -xzf tdl_Linux_64bit.tar.gz \
    && mv tdl /usr/local/bin/tdl \
    && chmod +x /usr/local/bin/tdl \
    && rm tdl_Linux_64bit.tar.gz

# Install ffmpeg as a static binary
RUN wget -q https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
    && tar -xf ffmpeg-master-latest-linux64-gpl.tar.xz \
    && mv ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /usr/local/bin/ffmpeg \
    && mv ffmpeg-master-latest-linux64-gpl/bin/ffprobe /usr/local/bin/ffprobe \
    && rm -rf ffmpeg-master-latest-linux64-gpl* \
    && ffmpeg -version
    
WORKDIR /app
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --upgrade pip  
RUN pip install -r requirements.txt
RUN pip install tgcrypto 

COPY . .
CMD ["python", "main/bot.py"]