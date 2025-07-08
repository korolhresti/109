-- Таблиця користувачів
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_admin BOOLEAN DEFAULT FALSE,
    last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    language TEXT DEFAULT 'uk',
    auto_notifications BOOLEAN DEFAULT FALSE,
    digest_frequency TEXT DEFAULT 'daily',
    safe_mode BOOLEAN DEFAULT FALSE,
    current_feed_id INTEGER, -- Може бути NULL
    is_premium BOOLEAN DEFAULT FALSE,
    premium_expires_at TIMESTAMP WITH TIME ZONE, -- Може бути NULL
    level INTEGER DEFAULT 1,
    badges JSONB DEFAULT '[]'::jsonb, -- Зберігаємо як JSONB для гнучкості
    inviter_id INTEGER REFERENCES users(id) ON DELETE SET NULL, -- Може бути NULL
    view_mode TEXT DEFAULT 'detailed',
    premium_invite_count INTEGER DEFAULT 0,
    digest_invite_count INTEGER DEFAULT 0,
    is_pro BOOLEAN DEFAULT FALSE,
    ai_requests_today INTEGER DEFAULT 0,
    ai_last_request_date DATE DEFAULT CURRENT_DATE,
    preferred_language TEXT DEFAULT 'uk' -- НОВА КОЛОНКА: мова для перекладу новин
);

-- Таблиця джерел новин
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    language TEXT DEFAULT 'uk',
    status TEXT DEFAULT 'active', -- 'active', 'inactive'
    last_parsed_at TIMESTAMP WITH TIME ZONE,
    parse_interval_minutes INTEGER DEFAULT 60 -- Як часто парсити це джерело
);

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT,
    source_url TEXT UNIQUE NOT NULL,
    image_url TEXT,
    published_at TIMESTAMP WITH TIME ZONE,
    lang TEXT DEFAULT 'uk',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Додаємо колонку is_sent до таблиці news, якщо її ще немає
ALTER TABLE news ADD COLUMN IF NOT EXISTS is_sent BOOLEAN DEFAULT FALSE;

-- Таблиця для зберігання переглядів новин користувачами
CREATE TABLE IF NOT EXISTS user_news_views (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE,
    viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Таблиця для зберігання реакцій користувачів на новини
CREATE TABLE IF NOT EXISTS user_news_reactions (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE,
    reaction_type TEXT NOT NULL, -- 'like', 'dislike'
    reacted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Таблиця для зберігання закладок користувачів
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Таблиця для зберігання звітів (наприклад, про нерелевантні новини)
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    news_id INTEGER REFERENCES news(id) ON DELETE SET NULL,
    report_type TEXT NOT NULL, -- 'irrelevant', 'inaccurate', 'spam'
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending' -- 'pending', 'reviewed', 'resolved'
);

-- Таблиця для зберігання запрошень (для преміум, дайджесту тощо)
CREATE TABLE IF NOT EXISTS invitations (
    id SERIAL PRIMARY KEY,
    inviter_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    invitee_telegram_id BIGINT UNIQUE,
    invite_code TEXT UNIQUE NOT NULL,
    invite_type TEXT NOT NULL, -- 'premium', 'digest'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE,
    used_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at TIMESTAMP WITH TIME ZONE
);

-- Таблиця для зберігання статистики джерел
CREATE TABLE IF NOT EXISTS source_stats (
    source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    news_count INTEGER DEFAULT 0,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця для зберігання підписок користувачів на джерела
CREATE TABLE IF NOT EXISTS user_subscriptions (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    subscribed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, source_id)
);

-- Таблиця для кешування RSS-стрічок (якщо буде використовуватися)
CREATE TABLE IF NOT EXISTS rss_cache (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    last_hash TEXT NOT NULL,
    last_checked TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця для зберігання черги завдань (наприклад, для відкладеного парсингу)
CREATE TABLE IF NOT EXISTS task_queue (
    id SERIAL PRIMARY KEY,
    task_type TEXT NOT NULL, -- 'parse_source', 'send_digest', etc.
    task_data JSONB NOT NULL, -- Дані, необхідні для виконання завдання
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'completed', 'failed'
    processed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT
);

-- Таблиця для зберігання налаштувань бота
CREATE TABLE IF NOT EXISTS bot_settings (
    id SERIAL PRIMARY KEY,
    setting_key TEXT UNIQUE NOT NULL,
    setting_value TEXT NOT NULL,
    description TEXT
);

-- Додавання початкових налаштувань, якщо вони ще не існують
INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('DEFAULT_PARSE_INTERVAL_MINUTES', '60', 'Інтервал парсингу за замовчуванням для нових джерел')
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('MAX_AI_REQUESTS_PER_DAY', '5', 'Максимальна кількість AI-запитів на день для звичайних користувачів')
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('NEWS_PUBLISH_INTERVAL_MINUTES', '5', 'Інтервал публікації новин в канал')
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('NEWS_PARSE_INTERVAL_MINUTES', '15', 'Інтервал парсингу новин з джерел')
ON CONFLICT (setting_key) DO NOTHING;
