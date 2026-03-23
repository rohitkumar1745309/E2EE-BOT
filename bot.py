# Updated bot.py with MULTI-COOKIES support

import os
import sys
import asyncio
import threading
import time
import json
import random
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

# Configuration
BOT_TOKEN = "8657735454:AAEzdYrevZhZu32XCTDvRuysg6gr1ejCnJc"
OWNER_FB_LINK = "https://www.facebook.com/profile.php?id=61588381456245"
SECRET_KEY = "TERI MA KI CHUT MDC"
CODE = "03102003"
MAX_TASKS = 100
PORT = 4000

DB_PATH = Path(__file__).parent / 'bot_data.db'
ENCRYPTION_KEY_FILE = Path(__file__).parent / '.encryption_key'

# Encryption setup
def get_encryption_key():
    if ENCRYPTION_KEY_FILE.exists():
        with open(ENCRYPTION_KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_data(data):
    if not data:
        return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except:
        return ""

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT UNIQUE NOT NULL,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            secret_key_verified INTEGER DEFAULT 0
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,
            telegram_id TEXT NOT NULL,
            cookies_encrypted TEXT,
            chat_id TEXT,
            name_prefix TEXT,
            messages TEXT,
            delay INTEGER DEFAULT 30,
            status TEXT DEFAULT 'stopped',
            messages_sent INTEGER DEFAULT 0,
            current_cookie_index INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

@dataclass
class Task:
    task_id: str
    telegram_id: str
    cookies: List[str]  # Changed to List for multi-cookies
    chat_id: str
    name_prefix: str
    messages: List[str]
    delay: int
    status: str
    messages_sent: int
    current_cookie_index: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
    running: bool = False
    stop_flag: bool = False
    
    def get_uptime(self):
        if not self.start_time:
            return "00:00:00"
        delta = datetime.now() - self.start_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.task_threads: Dict[str, threading.Thread] = {}
        self.load_tasks_from_db()
        self.start_auto_resume()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT task_id, telegram_id, cookies_encrypted, chat_id, name_prefix, messages, 
                   delay, status, messages_sent, current_cookie_index, start_time, last_active
            FROM tasks
        ''')
        for row in cursor.fetchall():
            try:
                cookies = json.loads(decrypt_data(row[2])) if row[2] else []
                messages = json.loads(decrypt_data(row[5])) if row[5] else []
                
                task = Task(
                    task_id=row[0],
                    telegram_id=row[1],
                    cookies=cookies,
                    chat_id=row[3] or "",
                    name_prefix=row[4] or "",
                    messages=messages,
                    delay=row[6] or 30,
                    status=row[7] or "stopped",
                    messages_sent=row[8] or 0,
                    current_cookie_index=row[9] or 0,
                    start_time=datetime.fromisoformat(row[10]) if row[10] else None,
                    last_active=datetime.fromisoformat(row[11]) if row[11] else None
                )
                self.tasks[task.task_id] = task
            except Exception as e:
                logger.error(f"Error loading task {row[0]}: {e}")
        conn.close()
        logger.info(f"Loaded {len(self.tasks)} tasks from database")
    
    def save_task(self, task: Task):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks 
            (task_id, telegram_id, cookies_encrypted, chat_id, name_prefix, messages, 
             delay, status, messages_sent, current_cookie_index, start_time, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id,
            task.telegram_id,
            encrypt_data(json.dumps(task.cookies)),
            task.chat_id,
            task.name_prefix,
            encrypt_data(json.dumps(task.messages)),
            task.delay,
            task.status,
            task.messages_sent,
            task.current_cookie_index,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None
        ))
        conn.commit()
        conn.close()
    
    def delete_task(self, task_id: str):
        if task_id in self.tasks:
            self.stop_task(task_id)
            del self.tasks[task_id]
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            conn.commit()
            conn.close()
            return True
        return False
    
    def start_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        if task.status == "running":
            return False
        if len([t for t in self.tasks.values() if t.status == "running"]) >= MAX_TASKS:
            return False
        task.status = "running"
        task.stop_flag = False
        if not task.start_time:
            task.start_time = datetime.now()
        task.last_active = datetime.now()
        self.save_task(task)
        
        thread = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        thread.start()
        self.task_threads[task_id] = thread
        return True
    
    def stop_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        task.stop_flag = True
        task.status = "stopped"
        task.last_active = datetime.now()
        self.save_task(task)
        return True
    
    def _run_task(self, task_id: str):
        task = self.tasks[task_id]
        task.running = True
        
        while task.status == "running" and not task.stop_flag:
            try:
                self._send_messages_with_rotation(task)
            except Exception as e:
                logger.error(f"Task {task_id} error: {e}")
                time.sleep(5)
        
        task.running = False
        if task_id in self.task_threads:
            del self.task_threads[task_id]
    
    def _send_messages_with_rotation(self, task: Task):
        """Send messages with multi-cookie rotation"""
        driver = None
        message_rotation_index = 0
        cookie_index = task.current_cookie_index
        
        try:
            # Loop through all cookies one by one
            while task.status == "running" and not task.stop_flag:
                # Get current cookie
                current_cookie = task.cookies[cookie_index % len(task.cookies)]
                
                # Setup browser with this cookie
                driver = self._setup_browser()
                
                # Login with cookie
                if not self._login_with_cookie(driver, current_cookie):
                    logger.error(f"Failed to login with cookie {cookie_index}")
                    driver.quit()
                    cookie_index += 1
                    task.current_cookie_index = cookie_index
                    self.save_task(task)
                    time.sleep(10)
                    continue
                
                # Send messages with this cookie
                result = self._send_messages_with_driver(driver, task, message_rotation_index)
                message_rotation_index = result['message_index']
                task.messages_sent += result['sent_count']
                self.save_task(task)
                
                # Close browser for this cookie
                driver.quit()
                driver = None
                
                # Move to next cookie
                cookie_index += 1
                task.current_cookie_index = cookie_index
                self.save_task(task)
                
                # Small delay between cookie switches
                time.sleep(5)
                
        except Exception as e:
            logger.error(f"Multi-cookie send error: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
    
    def _login_with_cookie(self, driver, cookie_string):
        """Login using cookie string"""
        try:
            driver.get('https://www.facebook.com/')
            time.sleep(5)
            
            # Clear existing cookies
            driver.delete_all_cookies()
            
            # Add cookies
            cookie_array = cookie_string.split(';')
            for cookie in cookie_array:
                cookie_trimmed = cookie.strip()
                if cookie_trimmed and '=' in cookie_trimmed:
                    name, value = cookie_trimmed.split('=', 1)
                    try:
                        driver.add_cookie({
                            'name': name.strip(),
                            'value': value.strip(),
                            'domain': '.facebook.com',
                            'path': '/'
                        })
                    except Exception as e:
                        logger.debug(f"Cookie add error: {e}")
            
            # Refresh to apply cookies
            driver.get('https://www.facebook.com/')
            time.sleep(5)
            
            # Check if logged in (look for profile or messenger)
            page_source = driver.page_source
            if 'messenger' in page_source.lower() or 'profile' in page_source.lower() or 'home' in page_source.lower():
                return True
            return False
            
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False
    
    def _send_messages_with_driver(self, driver, task: Task, start_message_index):
        """Send messages using existing driver - EXACT SAME as main.py"""
        message_rotation_index = start_message_index
        messages_sent = 0
        
        try:
            # Open conversation
            if task.chat_id:
                driver.get(f'https://www.facebook.com/messages/t/{task.chat_id}')
            else:
                driver.get('https://www.facebook.com/messages')
            
            time.sleep(15)
            
            # Find message input
            message_input = self._find_message_input(driver)
            
            if not message_input:
                return {'sent_count': 0, 'message_index': message_rotation_index}
            
            messages_list = [msg.strip() for msg in task.messages if msg.strip()]
            if not messages_list:
                messages_list = ['Hello!']
            
            # Send messages with current cookie
            while task.status == "running" and not task.stop_flag:
                # Get next message
                base_message = messages_list[message_rotation_index % len(messages_list)]
                message_rotation_index += 1
                
                # Add name prefix
                if task.name_prefix:
                    message_to_send = f"{task.name_prefix} {base_message}"
                else:
                    message_to_send = base_message
                
                try:
                    # EXACT SAME JavaScript from main.py
                    driver.execute_script("""
                        const element = arguments[0];
                        const message = arguments[1];
                        
                        element.scrollIntoView({behavior: 'smooth', block: 'center'});
                        element.focus();
                        element.click();
                        
                        if (element.tagName === 'DIV') {
                            element.textContent = message;
                            element.innerHTML = message;
                        } else {
                            element.value = message;
                        }
                        
                        element.dispatchEvent(new Event('input', { bubbles: true }));
                        element.dispatchEvent(new Event('change', { bubbles: true }));
                        element.dispatchEvent(new InputEvent('input', { bubbles: true, data: message }));
                    """, message_input, message_to_send)
                    
                    time.sleep(1)
                    
                    # Send button or Enter key
                    sent = driver.execute_script("""
                        const sendButtons = document.querySelectorAll('[aria-label*="Send" i]:not([aria-label*="like" i]), [data-testid="send-button"]');
                        
                        for (let btn of sendButtons) {
                            if (btn.offsetParent !== null) {
                                btn.click();
                                return 'button_clicked';
                            }
                        }
                        return 'button_not_found';
                    """)
                    
                    if sent == 'button_not_found':
                        driver.execute_script("""
                            const element = arguments[0];
                            element.focus();
                            
                            const events = [
                                new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }),
                                new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }),
                                new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true })
                            ];
                            
                            events.forEach(event => element.dispatchEvent(event));
                        """, message_input)
                    
                    messages_sent += 1
                    task.last_active = datetime.now()
                    
                    time.sleep(task.delay)
                    
                except Exception as e:
                    logger.error(f"Send error: {str(e)[:100]}")
                    time.sleep(5)
            
        except Exception as e:
            logger.error(f"Send messages error: {e}")
        
        return {'sent_count': messages_sent, 'message_index': message_rotation_index}
    
    def _setup_browser(self):
        """EXACT SAME as main.py"""
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        # Ghost mode - no active status
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        chromium_paths = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/chrome']
        for chromium_path in chromium_paths:
            if Path(chromium_path).exists():
                chrome_options.binary_location = chromium_path
                break
        
        try:
            from selenium.webdriver.chrome.service import Service
            driver = webdriver.Chrome(options=chrome_options)
            driver.set_window_size(1920, 1080)
            return driver
        except:
            return webdriver.Chrome(options=chrome_options)
    
    def _find_message_input(self, driver):
        """EXACT SAME as main.py"""
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(2)
        except:
            pass
        
        message_input_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
            'div[aria-label*="message" i][contenteditable="true"]',
            'div[aria-label*="Message" i][contenteditable="true"]',
            'div[contenteditable="true"][spellcheck="true"]',
            '[role="textbox"][contenteditable="true"]',
            'textarea[placeholder*="message" i]',
            'div[aria-placeholder*="message" i]',
            'div[data-placeholder*="message" i]',
            '[contenteditable="true"]',
            'textarea',
            'input[type="text"]'
        ]
        
        for selector in message_input_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        is_editable = driver.execute_script("""
                            return arguments[0].contentEditable === 'true' || 
                                   arguments[0].tagName === 'TEXTAREA' || 
                                   arguments[0].tagName === 'INPUT';
                        """, element)
                        
                        if is_editable:
                            try:
                                element.click()
                                time.sleep(0.5)
                            except:
                                pass
                            return element
                    except:
                        continue
            except:
                continue
        
        return None
    
    def start_auto_resume(self):
        def auto_resume():
            while True:
                try:
                    for task_id, task in self.tasks.items():
                        if task.status == "running" and not task.running:
                            self.start_task(task_id)
                except Exception as e:
                    logger.error(f"Auto resume error: {e}")
                time.sleep(60)
        
        thread = threading.Thread(target=auto_resume, daemon=True)
        thread.start()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('selenium').setLevel(logging.WARNING)

task_manager = TaskManager()

# User verification functions
def verify_user(telegram_id: str, secret_key: str = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if secret_key:
        if secret_key == SECRET_KEY:
            cursor.execute('INSERT OR REPLACE INTO users (telegram_id, secret_key_verified) VALUES (?, ?)', (telegram_id, 1))
            conn.commit()
            conn.close()
            return True
        return False
    
    cursor.execute('SELECT secret_key_verified FROM users WHERE telegram_id = ?', (telegram_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1

# Telegram bot handlers
async def start_command(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    if verify_user(user_id):
        await show_menu(update, context)
    else:
        await update.message.reply_text(
            f"Welcome to Raj Mishra end to end world\n\n"
            f"Please contact my owner: {OWNER_FB_LINK}\n\n"
            f"To get the secret key to start\n\n"
            f"Send the secret key to continue:"
        )

async def handle_secret_key(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    secret = update.message.text.strip()
    
    if verify_user(user_id, secret):
        await update.message.reply_text(
            "Welcome to New world\n\n"
            "Please choose option:\n\n"
            "A. Send cookies (one per line for multiple cookies)\n"
            "B. Send chat thread ID\n"
            "C. Send messages file (.txt)\n"
            "D. Send name prefix\n"
            "E. Send time delay\n"
            "F. Send code to start task\n"
            "G. Manage tasks\n\n"
            "Send the option letter to proceed:"
        )
        context.user_data['verified'] = True
        context.user_data['setup_step'] = 'awaiting_option'
    else:
        await update.message.reply_text(f"Code galat hai! Please visit my owner: {OWNER_FB_LINK}")

async def handle_option(update: Update, context: CallbackContext):
    option = update.message.text.strip().upper()
    
    if option == 'A':
        context.user_data['setup_step'] = 'awaiting_cookies'
        await update.message.reply_text(
            "Send your Facebook cookies (one per line for multiple cookies):\n\n"
            "Example for single cookie:\n"
            "c_user=1234567890; xs=789012%3Aabc123; datr=abc123\n\n"
            "Example for multiple cookies:\n"
            "c_user=111; xs=111; datr=111\n"
            "c_user=222; xs=222; datr=222\n"
            "c_user=333; xs=333; datr=333"
        )
    
    elif option == 'B':
        context.user_data['setup_step'] = 'awaiting_chat_id'
        await update.message.reply_text("Send chat thread ID:\n\nExample: 1362400298935018")
    
    elif option == 'C':
        context.user_data['setup_step'] = 'awaiting_messages'
        await update.message.reply_text("Send your messages file (.txt) with one message per line:")
    
    elif option == 'D':
        context.user_data['setup_step'] = 'awaiting_name_prefix'
        await update.message.reply_text("Send the name prefix:")
    
    elif option == 'E':
        context.user_data['setup_step'] = 'awaiting_delay'
        await update.message.reply_text("Send the time delay (in seconds):")
    
    elif option == 'F':
        context.user_data['setup_step'] = 'awaiting_code'
        await update.message.reply_text("Send the code to start the task:")
    
    elif option == 'G':
        context.user_data['setup_step'] = 'awaiting_task_action'
        await update.message.reply_text(
            "Send task ID to manage:\n\n"
            "Commands:\n"
            "/stop TASK_ID - Stop task\n"
            "/resume TASK_ID - Resume task\n"
            "/status TASK_ID - Check status\n"
            "/delete TASK_ID - Delete task\n"
            "/uptime TASK_ID - Check uptime\n"
            "/tasks - List all your tasks"
        )
    
    else:
        await update.message.reply_text("Invalid option! Please choose A, B, C, D, E, F, or G")

async def handle_cookies(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    # Split by newline for multiple cookies
    cookies = [c.strip() for c in text.split('\n') if c.strip()]
    
    if 'config' not in context.user_data:
        context.user_data['config'] = {}
    context.user_data['config']['cookies'] = cookies
    
    await update.message.reply_text(f"✅ {len(cookies)} cookie(s) saved!")
    context.user_data['setup_step'] = 'awaiting_option'
    await show_menu(update, context)

async def handle_chat_id(update: Update, context: CallbackContext):
    chat_id = update.message.text.strip()
    context.user_data['config']['chat_id'] = chat_id
    await update.message.reply_text(f"✅ Chat ID saved!")
    context.user_data['setup_step'] = 'awaiting_option'
    await show_menu(update, context)

async def handle_messages(update: Update, context: CallbackContext):
    if update.message.document:
        file = await update.message.document.get_file()
        file_content = await file.download_as_bytearray()
        messages = file_content.decode('utf-8').strip().split('\n')
        messages = [m.strip() for m in messages if m.strip()]
        
        context.user_data['config']['messages'] = messages
        await update.message.reply_text(f"✅ {len(messages)} message(s) loaded!")
        context.user_data['setup_step'] = 'awaiting_option'
        await show_menu(update, context)
    else:
        await update.message.reply_text("Please send the messages as a .txt file!")

async def handle_name_prefix(update: Update, context: CallbackContext):
    context.user_data['config']['name_prefix'] = update.message.text.strip()
    await update.message.reply_text("✅ Name prefix saved!")
    context.user_data['setup_step'] = 'awaiting_option'
    await show_menu(update, context)

async def handle_delay(update: Update, context: CallbackContext):
    try:
        delay = int(update.message.text.strip())
        context.user_data['config']['delay'] = delay
        await update.message.reply_text(f"✅ Delay set to {delay} seconds!")
        context.user_data['setup_step'] = 'awaiting_option'
        await show_menu(update, context)
    except:
        await update.message.reply_text("Invalid number! Please send a valid number.")

async def handle_code(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    code = update.message.text.strip()
    
    if code == CODE:
        config = context.user_data.get('config', {})
        
        required = ['cookies', 'chat_id', 'messages', 'name_prefix', 'delay']
        if not all(k in config for k in required):
            await update.message.reply_text("Please complete all setup steps (A-E) before sending the code!")
            return
        
        task_id = f"rajmishra_{random.randint(10000, 99999)}"
        
        task = Task(
            task_id=task_id,
            telegram_id=user_id,
            cookies=config['cookies'],
            chat_id=config['chat_id'],
            name_prefix=config['name_prefix'],
            messages=config['messages'],
            delay=config['delay'],
            status="stopped",
            messages_sent=0,
            current_cookie_index=0,
            start_time=None,
            last_active=None
        )
        
        task_manager.tasks[task_id] = task
        task_manager.save_task(task)
        task_manager.start_task(task_id)
        
        await update.message.reply_text(
            f"✅ Task started!\n\n"
            f"Task ID: {task_id}\n"
            f"Cookies: {len(config['cookies'])} cookie(s)\n"
            f"Status: Running\n"
            f"Use /status {task_id} to check progress"
        )
        
        context.user_data['config'] = {}
        context.user_data['setup_step'] = 'awaiting_option'
        await show_menu(update, context)
    else:
        await update.message.reply_text(f"Code galat hai! Please visit my owner: {OWNER_FB_LINK}")

async def show_menu(update: Update, context: CallbackContext):
    menu = (
        "📋 Main Menu:\n\n"
        "A. Send cookies (one per line)\n"
        "B. Send chat thread ID\n"
        "C. Send messages file\n"
        "D. Send name prefix\n"
        "E. Send time delay\n"
        "F. Send code to start task\n"
        "G. Manage tasks\n\n"
        "Send the option letter to proceed:"
    )
    await update.message.reply_text(menu)

async def stop_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Please provide task ID: /stop TASK_ID")
        return
    
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    
    if task_manager.tasks[task_id].telegram_id != user_id:
        await update.message.reply_text("You don't own this task!")
        return
    
    if task_manager.stop_task(task_id):
        await update.message.reply_text(f"✅ Task {task_id} stopped!")

async def resume_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Please provide task ID: /resume TASK_ID")
        return
    
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    
    if task_manager.tasks[task_id].telegram_id != user_id:
        await update.message.reply_text("You don't own this task!")
        return
    
    if task_manager.start_task(task_id):
        await update.message.reply_text(f"✅ Task {task_id} resumed!")

async def status_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Please provide task ID: /status TASK_ID")
        return
    
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    
    task = task_manager.tasks[task_id]
    if task.telegram_id != user_id:
        await update.message.reply_text("You don't own this task!")
        return
    
    status_text = (
        f"📊 Task: {task_id}\n\n"
        f"Status: {task.status}\n"
        f"Messages Sent: {task.messages_sent}\n"
        f"Cookies: {len(task.cookies)} (Current: {task.current_cookie_index % len(task.cookies) + 1})\n"
        f"Chat ID: {task.chat_id}\n"
        f"Name Prefix: {task.name_prefix}\n"
        f"Messages: {len(task.messages)}\n"
        f"Delay: {task.delay}s\n"
        f"Uptime: {task.get_uptime()}"
    )
    await update.message.reply_text(status_text)

async def delete_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Please provide task ID: /delete TASK_ID")
        return
    
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    
    if task_manager.tasks[task_id].telegram_id != user_id:
        await update.message.reply_text("You don't own this task!")
        return
    
    if task_manager.delete_task(task_id):
        await update.message.reply_text(f"✅ Task {task_id} deleted!")

async def uptime_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Please provide task ID: /uptime TASK_ID")
        return
    
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    
    task = task_manager.tasks[task_id]
    if task.telegram_id != user_id:
        await update.message.reply_text("You don't own this task!")
        return
    
    await update.message.reply_text(f"⏱️ Task {task_id} uptime: {task.get_uptime()}")

async def list_tasks_command(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    user_tasks = [t for t in task_manager.tasks.values() if t.telegram_id == user_id]
    
    if not user_tasks:
        await update.message.reply_text("No tasks found!")
        return
    
    tasks_list = "📋 Your Tasks:\n\n"
    for task in user_tasks:
        tasks_list += f"ID: {task.task_id}\n"
        tasks_list += f"Status: {task.status}\n"
        tasks_list += f"Cookies: {len(task.cookies)}\n"
        tasks_list += f"Sent: {task.messages_sent}\n"
        tasks_list += f"Uptime: {task.get_uptime()}\n"
        tasks_list += "---\n"
    
    await update.message.reply_text(tasks_list)

# Health check server
def health_check():
    import socket
    class HealthServer:
        def __init__(self, port=4000):
            self.port = port
        def start(self):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', self.port))
            sock.listen(5)
            while True:
                try:
                    client, _ = sock.accept()
                    client.send(b"HTTP/1.1 200 OK\r\n\r\nOK")
                    client.close()
                except:
                    pass
    threading.Thread(target=HealthServer(PORT).start, daemon=True).start()

async def handle_message(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()
    
    if not verify_user(user_id) and text != SECRET_KEY:
        await start_command(update, context)
        return
    
    if text == SECRET_KEY:
        await handle_secret_key(update, context)
        return
    
    step = context.user_data.get('setup_step', 'awaiting_option')
    
    if step == 'awaiting_option':
        await handle_option(update, context)
    elif step == 'awaiting_cookies':
        await handle_cookies(update, context)
    elif step == 'awaiting_chat_id':
        await handle_chat_id(update, context)
    elif step == 'awaiting_name_prefix':
        await handle_name_prefix(update, context)
    elif step == 'awaiting_delay':
        await handle_delay(update, context)
    elif step == 'awaiting_code':
        await handle_code(update, context)
    else:
        await show_menu(update, context)

def main():
    health_check()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_task_command))
    application.add_handler(CommandHandler("resume", resume_task_command))
    application.add_handler(CommandHandler("status", status_task_command))
    application.add_handler(CommandHandler("delete", delete_task_command))
    application.add_handler(CommandHandler("uptime", uptime_task_command))
    application.add_handler(CommandHandler("tasks", list_tasks_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_messages))
    
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
