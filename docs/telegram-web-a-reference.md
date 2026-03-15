# Telegram Web A — Design Reference

Extracted from `web.telegram.org/a` (theme-dark, purple accent) for future UI work.

## Meta Tags (Mobile PWA)
```html
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,shrink-to-fit=no,viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#212121">
```

## CSS Variables — Dark Theme (Purple Accent)

### Core
| Variable | Value | Usage |
|----------|-------|-------|
| `--color-primary` | `rgb(135,116,225)` | Main accent (purple) |
| `--color-primary-shade` | `rgb(123,113,198)` | Darker accent |
| `--color-background` | `rgb(33,33,33)` | Main bg (#212121) |
| `--color-background-secondary` | `rgb(15,15,15)` | Sidebar bg (#0f0f0f) |
| `--color-background-own` | `rgb(118,106,200)` | Own message bubble |
| `--color-text` | `rgb(255,255,255)` | Primary text |
| `--color-text-secondary` | `rgb(170,170,170)` | Muted text |
| `--color-borders` | `rgb(48,48,48)` | Border color |
| `--color-dividers` | `rgb(59,59,61)` | Divider lines |
| `--color-links` | `rgb(135,116,225)` | Link color (= primary) |

### Chat Selection
| Variable | Value |
|----------|-------|
| `--color-chat-hover` | `rgb(44,44,44)` |
| `--color-chat-active` | `rgb(118,106,200)` |
| `--color-item-hover` | `rgb(44,44,44)` |
| `--color-background-selected` | `rgb(44,44,44)` |

### Messages
| Variable | Value |
|----------|-------|
| `--color-background-own-selected` | `rgb(101,73,212)` |
| `--color-reply-hover` | `rgb(39,39,39)` |
| `--color-reply-own-hover` | `rgb(135,117,218)` |
| `--color-accent-own` | `rgb(255,255,255)` |
| `--color-message-meta-own` | `rgba(255,255,255,0.533)` |

### Reactions
| Variable | Value |
|----------|-------|
| `--color-message-reaction` | `rgb(43,42,53)` |
| `--color-message-reaction-own` | `rgb(103,92,175)` |

## Peer Colors (7 base + 14 extended)
```
--color-peer-0: #D45246  (red)
--color-peer-1: #F68136  (orange)
--color-peer-2: #6C61DF  (purple)
--color-peer-3: #46BA43  (green)
--color-peer-4: #5CAFFA  (light blue)
--color-peer-5: #408ACF  (blue)
--color-peer-6: #D95574  (pink)
```

## Layout Structure
```
body#root
  #portals (modals, tooltips)
  .Transition (main wrapper)
    #Main
      #LeftColumn (sidebar, 366px default)
        .LeftMainHeader (menu + search)
        .ChatFolders (tab bar: All, Unread, etc.)
        .chat-list (virtual scroll)
        .NewChatButton (FAB)
      #MiddleColumn (messages area)
      #RightColumn-wrapper (profile/info panel)
```

## Key Design Patterns
- **Virtual scroll** for chat list (absolute positioned items with `style="top: Npx"`)
- **Avatar system**: 54px in list, color-coded by peer-id, story ring overlay
- **Tab bar** with animated underline indicator
- **Glassmorphism** menus: `backdrop-filter: blur()` with semi-transparent bg
- **Status badges**: online dot (green), typing indicator, unread count
- **Touch targets**: all interactive elements min 44px
- **Font stack**: system-ui, Roboto, Helvetica Neue, sans-serif
- **Message text size**: configurable via `--message-text-size` (default 16px)
