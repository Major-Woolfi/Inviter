# 📧 Inviter - бот для пиара нового поколения

[![Stars](https://img.shields.io/github/stars/Major-Woolfi/Inviter?style=social)](https://github.com/Major-Woolfi/Inviter/stargazers)
[![Issues](https://img.shields.io/github/issues/Major-Woolfi/Inviter)](https://github.com/Major-Woolfi/Inviter/issues)
[![License](https://img.shields.io/github/license/Major-Woolfi/Inviter)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Major-Woolfi/.github/blob/main/community/CONTRIBUTING.md)
![Status](https://img.shields.io/badge/Status-active-brightgreen)

[🐛 Сообщить о баге](https://github.com/Major-Woolfi/Inviter/issues/new) •
[💡 Предложить идею](https://github.com/Major-Woolfi/Inviter/discussions)

---

## 📑 Содержание

- [📧 Inviter - бот для пиара нового поколения](#-inviter---бот-для-пиара-нового-поколения)
  - [📑 Содержание](#-содержание)
  - [📖 Описание проекта](#-описание-проекта)
    - [Идея и концепция](#идея-и-концепция)
    - [Полное описание](#полное-описание)
    - [Для кого этот проект](#для-кого-этот-проект)
  - [✨ Реализованные фичи](#-реализованные-фичи)
  - [🚀 Быстрый старт](#-быстрый-старт)
    - [Предварительные требования](#предварительные-требования)
    - [Установка](#установка)
    - [Конфигурация](#конфигурация)
    - [Деплой](#деплой)
  - [🏗️ Архитектура проекта](#️-архитектура-проекта)
  - [🛠️ Технологический стек](#️-технологический-стек)
  - [📊 Статистика проекта](#-статистика-проекта)
  - [🤝 Контрибьюция](#-контрибьюция)
  - [👥 Авторы и благодарности](#-авторы-и-благодарности)
  - [📄 Лицензия](#-лицензия)

## 📖 Описание проекта

### Идея и концепция

**Inviter** — это проект, созданный для решения целого спектра задач:

- Перегонка трафика `из` - `в`
- Рассылки пользователям в личные сообщения
- Инвайтинг живого, реального трафика
- Продвинутая автоматизация пиара любым способом
- Удешивление живого трафика и рекламы
- Продвижение своих продуктов и/или себя самого

Основная идея проекта родилась из необходимости продвижения моих продуктов. Проект воплощает подход автоматизации, упрощения, ускорения и удешивления рутинных задач, что позволяет добится максимального результата за минимальный промежуток времени.

### Полное описание

Inviter - проект продвинутого бота-инвайтера, который умеет не только добавлять пользователей из одного чата в другой, но также может рассыласть рекламу как в чаты так и в ЛС, выбирает всегда реально активных и живых пользователей, имеет фильтры и повышенный уровень легитимности относительно Anti-bot систем.

Проект ориентирован на траферов и решает следующие задачи:

- Пиар
- Рассылки
- Реклама
- Инвайтинг

Ключевые принципы проекта:

1. **Простота** - упрощение сложных процессов
2. **Удобство** - сделать выполнение задачи максимально удобным
3. **Надёжность** - максимальная устойчивость к любому сценарию использования
4. **Гибкость** - максимально гибкие настройки

### Для кого этот проект

- Траферы
- Индивидуальные пользователи
- Администраторы любых проектов

> Но на этом аудитория не ограничивается. Она ограничивается лишь вашей фантазией.

---

## ✨ Реализованные фичи

Реализовано и работоспособно:

- Инвайтинг
- БД чатов
- Валидаторы
- Human-like поведение
- Рандомизаторы
- Гибкие настройки
- Отказоустойчивость
- Кэширование

---

## 🚀 Быстрый старт

### Предварительные требования

- Зависимости
  - Runtime - Python 3.13.9+ (возможно и старше, тестирование не проводилось)
  - Библиотеки из requirements.txt
- Железо (выделеное под бота, минимум для запуска и корректной работы)
  - CPU 1 ядро 1Ггц
  - RAM 200мб
  - ROM 250мб

### Установка

```shell
# Клонируйте репозиторий
git clone https://github.com/Major-Woolfi/Inviter.git
cd Inviter
```

### Конфигурация

```shell
# Скопируйте файл переменных окружения
cp .env.example .env
# Отредактируйте .env под ваши нужды
```

### Деплой

```shell
cd /root/bots/Inviter_bot/ || exit 1

docker build -t inviter_bot .
docker rm -f inviter_bot 2>/dev/null || true
docker run -d --name inviter_bot --restart always -v "$(pwd)":/app inviter_bot

echo "✅ Deployed! Logs: docker logs -f  inviter_bot "
```

> Вам нужно изменить путь в `cd` или переместить бота в соответствующую папку

---

## 🏗️ Архитектура проекта

```plaintext
├── data/
│  └── ...
├── langs/
│  ├── en.json
│  └── ru.json
├── logs/
│  └── ...
├── sessions/
│  └── ...
├── .env
├── .env.example
├── main.py
├── requirements.txt
├── Dockerfile
├── deploy_bot.sh
├── .gitignore
├── LICENSE
└── README.md
```

---

## 🛠️ Технологический стек

| Категория    | Технологии          |
| ------------ | ------------------- |
| **Backend**  | telethon            |
| **Frontend** | aiogram             |
| **Database** | aiofiles, aiosqlite |
| **DevOps**   | Docker              |

> В `Backend`, `Frontend` и `Database` указаны библиотеки Python, т.к. это единственный язык который тут используется не считая языков на которых написаны сами библиотеки.

---

## 📊 Статистика проекта

| Метрика        | Значение                                                                                                                                        |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| ⭐ Stars        | [![Stars](https://img.shields.io/github/stars/Major-Woolfi/Inviter)](https://github.com/Major-Woolfi/Inviter/stargazers)                        |
| 🍴 Forks        | [![Forks](https://img.shields.io/github/forks/Major-Woolfi/Inviter)](https://github.com/Major-Woolfi/REPO_NAME/network/members)                 |
| 🐛 Issues       | [![Issues](https://img.shields.io/github/issues/Major-Woolfi/Inviter)](https://github.com/Major-Woolfi/Inviter/issues)                          |
| 👥 Contributors | [![Contributors](https://img.shields.io/github/contributors/Major-Woolfi/Inviter)](https://github.com/Major-Woolfi/Inviter/graphs/contributors) |

---

## 🤝 Контрибьюция

Приветствуем любые вклад в проект! Перед созданием PR обязательно прочитай:

- 📋 [CONTRIBUTING](https://github.com/Major-Woolfi/.github/blob/main/community/CONTRIBUTING.md) — правила участия
- 💬 [CODE OF CONDUCT](https://github.com/Major-Woolfi/.github/blob/main/community/CODE_OF_CONDUCT.md) — кодекс поведения
- 🐛 [ISSUE TEMPLATE](https://github.com/Major-Woolfi/.github/tree/main/community/ISSUES.md) — шаблоны багов и фич
- 🔀 [PULL REQUEST TEMPLATE](https://github.com/Major-Woolfi/.github/blob/main/community/PULL_REQUEST_TEMPLATE.md) — требования к PR

Все общие правила хранятся в [репозитории `.github`](https://github.com/Major-Woolfi/.github) в папке `community`.

---

## 👥 Авторы и благодарности

<a href="https://github.com/Major-Woolfi/Inviter/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Major-Woolfi/Inviter" />
</a>

---

## 📄 Лицензия

Этот проект распространяется под лицензией **MIT**. Подробности в файле [LICENSE](LICENSE).

---

**⭐ Поставь звезду, если проект понравился!**

**[📧 Telegram](https://Major_Woolfi.t.me)**
