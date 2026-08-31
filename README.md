# Инструмент сравнения потоков задач (Flow Compare)

## 1. Описание проекта

Проект представляет собой интерактивное веб-приложение на базе фреймворка **Streamlit**, предназначенное для загрузки, нормализации, сравнения и визуализации зависимостей и параметров расписания (conditions) ETL-потоков между двумя кластерами: ADH1 и ADH3.

### Как функционирует проект
1. **Сбор данных (`src/get_json.py`)**: Скрипт считывает список потоков из текстовых файлов (`data/wf_list_adh1.txt` и `data/wf_list_adh3.txt`), делает HTTP API-запросы к серверу `adp-eiap-app1.adp.local:8191` и сохраняет извлеченные параметры запуска (conditions) в локальные JSON-файлы (`data/adh1_new.json` и `data/adh3_new.json`).
2. **Веб-интерфейс (`src/app.py`)**: Streamlit-приложение парсит эти JSON-файлы и разворачивает сложные вложенные структуры (например, целевые сущности `parentIsNotWorkingByEntity` или интервалы расписаний `execTimePeriod`) в плоские таблицы Pandas. 
3. **Интеграция с БД**: Приложение использует `psycopg2` для подключения к PostgreSQL (БД `mdb`), чтобы сопоставлять аналоги родительских потоков и сущностей по их метаданным.
4. **Анализ и экспорт**: Пользователь через удобный интерфейс может просматривать зависимости, фильтровать их, проводить ручную проверку соответствия (с сохранением результатов) и выгружать параметры расписания (CSV) для дальнейшего документирования.

### Процесс запуска в режиме разработки
1. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
2. Подготовьте данные, запустив сборщик (опционально, если JSON файлы еще не загружены):
   ```bash
   python src/get_json.py
   ```
3. Запустите Streamlit приложение:
   ```bash
   streamlit run src/app.py
   ```

---

## 2. Сборка приложения в исполняемый файл (.exe)

Для удобного запуска приложения на других ПК без установки Python используется библиотека **PyInstaller**.
Поскольку Streamlit использует динамические импорты и скрытую статику, процесс сборки требует специального скрипта-обертки и файла конфигурации.

### Этап 1: Создание скрипта-обертки (`run_main.py`)
Создайте файл `run_main.py` в корне проекта. Этот файл решает две проблемы:
1. Выполняет "фиктивные импорты" (dummy imports) всех используемых сторонних библиотек, чтобы PyInstaller гарантированно запаковал их внутрь `.exe`.
2. Правильно определяет путь к `src/app.py` при запуске из распакованной временной папки `sys._MEIPASS`.

**Содержимое `run_main.py`:**
```python
import sys
import os

# === ФИКТИВНЫЕ ИМПОРТЫ ДЛЯ PYINSTALLER ===
# PyInstaller увидит эти строчки и скопирует библиотеки в exe
import requests
import dotenv
import pandas
import streamlit
import psycopg2
# =========================================

import streamlit.web.cli as stcli

def resolve_path(path):
    if getattr(sys, 'frozen', False):
        resolved_path = os.path.abspath(os.path.join(sys._MEIPASS, path))
    else:
        resolved_path = os.path.abspath(os.path.join(os.getcwd(), path))
    return resolved_path

if __name__ == "__main__":
    app_path = resolve_path("src/app.py")
    sys.argv = ["streamlit", "run", app_path, "--global.developmentMode=false"]
    sys.exit(stcli.main())
```

### Этап 2: Создание и настройка `.spec` файла
Сначала сгенерируйте базовый файл спецификации:
```bash
pyinstaller --onefile run_main.py
```
После этого откройте созданный файл `run_main.spec` и отредактируйте его. Нужно добавить пути к статике Streamlit, рабочим папкам проекта и метаданным модулей.

**Ключевые изменения в `run_main.spec`:**
```python
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata
import streamlit

st_path = streamlit.__path__[0]

# Добавляем статику streamlit, исходники, data и .env
datas = [
    (f"{st_path}/static", "streamlit/static"),
    ("src", "src"),
    ("data", "data"),
    (".env", ".")
]
# Добавляем метаданные streamlit, чтобы не было ошибки PackageNotFoundError
datas += copy_metadata("streamlit")

a = Analysis(
    ['run_main.py'],
    pathex=[],
    binaries=[],
    datas=datas, # Обязательно укажите здесь переменную datas!
    hiddenimports=[
        'streamlit.runtime.scriptrunner.magic_funcs',
        'streamlit.runtime.scriptrunner',
    ],
    hookspath=[],
    # ... остальные параметры без изменений ...
)
# ... код сборки PYZ и EXE ...
```

### Этап 3: Финальная сборка
Запустите сборку, используя измененный файл спецификации:
```bash
pyinstaller run_main.spec --clean
```

После завершения в папке `dist/` появится файл `run_main.exe`. Вы можете переименовать его и использовать на других компьютерах Windows.
