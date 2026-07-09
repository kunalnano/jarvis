#!/bin/bash
# Jarvis Setup - Mac

echo "Setting up Jarvis AI Assistant..."

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements-mac.txt

# Copy config if not exists
if [ ! -f config/jarvis.yaml ]; then
    cp config/jarvis.mac.yaml config/jarvis.yaml
    echo ""
    echo "⚠️  Config created at config/jarvis.yaml"
    echo "   Edit it to add your ElevenLabs API key and Windows IP"
fi

echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit config/jarvis.yaml with your settings"
echo "2. Make sure LM Studio is running on Windows with 'Serve on Local Network' enabled"
echo "3. Run: ./start_jarvis.sh"
