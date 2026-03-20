# Telegram Web A — Design Reference

Extracted from web.telegram.org/a (dark theme, March 2026).

## Core Colors (Dark Theme)
- Primary: `rgb(135,116,225)` (#8774e1)
- Background: `rgb(33,33,33)` (#212121)
- Background Secondary: `rgb(15,15,15)` (#0f0f0f)
- Background Own Message: `rgb(118,106,200)` (#766ac8)
- Text: `rgb(255,255,255)` (#ffffff)
- Text Secondary: `rgb(170,170,170)` (#aaaaaa)
- Borders: `rgb(48,48,48)` (#303030)
- Links: `rgb(135,116,225)` (#8774e1)
- Chat Hover: `rgb(44,44,44)` (#2c2c2c)
- Chat Active: `rgb(118,106,200)` (#766ac8)
- Selected: `rgb(44,44,44)` (#2c2c2c)
- Dividers: `rgb(59,59,61)` (#3b3b3d)
- Code: `rgb(135,116,225)` (#8774e1)
- Green/Success: `rgb(0,199,62)` (#00c73e)

## Typography
- Font: System default (no custom font loaded beyond emoji)
- Chat list: name bold, last message regular, time secondary color
- Message text size: 16px (configurable)

## Layout
- Left column: 366px default width, resizable
- Chat list item: 72px height
- Avatar: 54px in chat list, 32px in story bar
- Message max-width: ~85%
- Right column: hidden by default, slides in

## Viewport
- `viewport-fit=cover` for notched phones
- `user-scalable=no` prevents pinch zoom
- Custom `--vh` variable for iOS Safari
- `--scrollbar-width: 10px`

## Key UI Patterns
- Tab bar for chat folders (horizontal scroll)
- Floating action button (bottom-right) for new chat
- Search input with icon left, avatar story ring
- Status indicators: online dot, read checks, typing indicator
- Archive row at top of chat list (collapsible)

## Peer Colors (7 base + extended)
Used for avatar backgrounds and sender names:
```
#D45246, #F68136, #6C61DF, #46BA43, #5CAFFA, #408ACF, #D95574
```
Extended dark variants with gradients available (14+ additional colors).
