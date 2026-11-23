#!/bin/bash
# Doble clic para abrir el Estudio (arranca el servidor local y abre el navegador).
cd "$(dirname "$0")"
exec python3 server.py
