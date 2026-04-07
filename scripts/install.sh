#!/bin/bash
# =============================================================================
# Car Data Collector Bot - Linux Kurulum Scripti
# Departman bilgisayarına 7/24 çalışacak şekilde kurulum
# =============================================================================

set -e

echo "========================================"
echo " Car Data Collector Bot - Kurulum"
echo "========================================"

# Renkler
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Değişkenler
INSTALL_DIR="/opt/datacollectorbot"
DATA_DIR="/data/car-collector"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_USER="datacollector"

# Root kontrolü
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}Bu script root olarak çalıştırılmalı (sudo)${NC}"
   exit 1
fi

# 1. Sistem bağımlılıkları
echo -e "${YELLOW}[1/8] Sistem bağımlılıkları kuruluyor...${NC}"
apt-get update
apt-get install -y python3 python3-pip python3-venv \
    build-essential libxml2-dev libxslt1-dev libffi-dev libssl-dev \
    curl wget

# 2. Servis kullanıcısı
echo -e "${YELLOW}[2/8] Servis kullanıcısı oluşturuluyor...${NC}"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false -m -d /home/$SERVICE_USER $SERVICE_USER
fi

# 3. Dizin yapısı
echo -e "${YELLOW}[3/8] Dizin yapısı oluşturuluyor...${NC}"
mkdir -p $INSTALL_DIR
mkdir -p $DATA_DIR/{raw/html,raw/pdf,processed,qa_output,temp,db}
mkdir -p $INSTALL_DIR/logs

# 4. Uygulama kopyalama
echo -e "${YELLOW}[4/8] Uygulama dosyaları kopyalanıyor...${NC}"
cp -r . $INSTALL_DIR/
chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR
chown -R $SERVICE_USER:$SERVICE_USER $DATA_DIR

# 5. Python sanal ortam
echo -e "${YELLOW}[5/8] Python sanal ortam oluşturuluyor...${NC}"
python3 -m venv $VENV_DIR
$VENV_DIR/bin/pip install --upgrade pip
$VENV_DIR/bin/pip install -r $INSTALL_DIR/requirements.txt

# 6. Ollama kurulumu
echo -e "${YELLOW}[6/8] Ollama kuruluyor...${NC}"
if ! command -v ollama &>/dev/null; then
    curl -fsSL https://ollama.ai/install.sh | sh
fi

# Modelleri indir
echo -e "${YELLOW}[6b/8] LLM modelleri indiriliyor (bu biraz sürebilir)...${NC}"
ollama pull gemma2:12b || echo -e "${YELLOW}Gemma2 indirilemedi, daha sonra deneyin${NC}"
ollama pull qwen2.5:14b || echo -e "${YELLOW}Qwen indirilemedi, daha sonra deneyin${NC}"

# 7. Systemd servisi
echo -e "${YELLOW}[7/8] Systemd servisi kuruluyor...${NC}"
cp $INSTALL_DIR/systemd/datacollector.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable datacollector.service

# 8. Konfigürasyon kontrolü
echo -e "${YELLOW}[8/8] Konfigürasyon kontrol ediliyor...${NC}"
if [[ ! -f $INSTALL_DIR/config/settings.yaml ]]; then
    echo -e "${RED}settings.yaml bulunamadı!${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}========================================"
echo " Kurulum Tamamlandı!"
echo "========================================"
echo ""
echo " Veri dizini:     $DATA_DIR"
echo " Uygulama dizini: $INSTALL_DIR"
echo " Log dizini:      $INSTALL_DIR/logs"
echo ""
echo " Komutlar:"
echo "   Başlat:  sudo systemctl start datacollector"
echo "   Durdur:  sudo systemctl stop datacollector"
echo "   Durum:   sudo systemctl status datacollector"
echo "   Loglar:  journalctl -u datacollector -f"
echo ""
echo " Manuel çalıştırma:"
echo "   cd $INSTALL_DIR"
echo "   $VENV_DIR/bin/python -m src.main run"
echo "   $VENV_DIR/bin/python -m src.main stats"
echo "   $VENV_DIR/bin/python -m src.main export-qa -o qa_data.jsonl"
echo ""
echo " Ek disk bağlama (TB'larca veri için):"
echo "   mount /dev/sdX1 $DATA_DIR"
echo "   # fstab'a ekle: /dev/sdX1 $DATA_DIR ext4 defaults 0 2"
echo "========================================${NC}"
