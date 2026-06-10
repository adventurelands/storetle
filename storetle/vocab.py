# vocab.py
# The "cube" — every known HTML5 tag and attribute, each given a number.
# This is the shared map that the encoder and decoder both use.
# It never gets stored in a compressed file — both sides already have it.

TAGS = [
    # Main structure
    'html', 'head', 'body', 'title', 'base', 'link', 'meta', 'style', 'script',
    'noscript', 'template', 'slot',
    # Sections
    'article', 'section', 'nav', 'aside', 'header', 'footer', 'main',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hgroup', 'address',
    # Grouping
    'p', 'hr', 'pre', 'blockquote', 'ol', 'ul', 'menu', 'li',
    'dl', 'dt', 'dd', 'figure', 'figcaption', 'div',
    # Inline text
    'a', 'em', 'strong', 'small', 's', 'cite', 'q', 'dfn', 'abbr',
    'ruby', 'rt', 'rp', 'data', 'time', 'code', 'var', 'samp', 'kbd',
    'sub', 'sup', 'i', 'b', 'u', 'mark', 'bdi', 'bdo', 'span', 'br', 'wbr',
    # Edits
    'ins', 'del',
    # Embedded content
    'picture', 'source', 'img', 'iframe', 'embed', 'object',
    'video', 'audio', 'track', 'map', 'area', 'canvas', 'svg',
    # SVG elements
    'path', 'circle', 'rect', 'line', 'polyline', 'polygon',
    'g', 'defs', 'use', 'symbol', 'clippath', 'lineargradient',
    'radialgradient', 'stop', 'pattern', 'text', 'tspan',
    # Tables
    'table', 'caption', 'colgroup', 'col', 'tbody', 'thead', 'tfoot',
    'tr', 'td', 'th',
    # Forms
    'form', 'label', 'input', 'button', 'select', 'datalist', 'optgroup',
    'option', 'textarea', 'output', 'progress', 'meter', 'fieldset', 'legend',
    # Interactive
    'details', 'summary', 'dialog', 'search',
]

ATTRS = [
    # Universal
    'id', 'class', 'style', 'title', 'lang', 'dir', 'tabindex',
    'accesskey', 'contenteditable', 'draggable', 'hidden', 'spellcheck',
    'translate', 'slot',
    # Links / sources
    'href', 'src', 'srcset', 'sizes', 'rel', 'target', 'download',
    'hreflang', 'type', 'media', 'crossorigin', 'referrerpolicy',
    'integrity', 'as',
    # Meta / head
    'name', 'content', 'charset', 'http-equiv', 'property',
    # Forms
    'action', 'method', 'enctype', 'novalidate', 'autocomplete',
    'for', 'form', 'value', 'placeholder', 'required', 'disabled',
    'readonly', 'checked', 'selected', 'multiple', 'autofocus',
    'pattern', 'min', 'max', 'step', 'minlength', 'maxlength',
    'list', 'accept', 'capture', 'wrap', 'rows', 'cols',
    # Media
    'autoplay', 'controls', 'loop', 'muted', 'poster', 'preload',
    'width', 'height', 'alt', 'loading', 'decoding',
    # Tables
    'colspan', 'rowspan', 'scope', 'headers', 'span',
    # Misc
    'open', 'reversed', 'start', 'async', 'defer', 'ismap', 'usemap',
    'coords', 'shape', 'datetime', 'cite', 'data', 'kind', 'label',
    'srclang', 'srcdoc', 'sandbox', 'allow', 'allowfullscreen',
    'formaction', 'formmethod', 'formenctype', 'formnovalidate', 'formtarget',
    'high', 'low', 'optimum', 'default', 'align',
    # SVG
    'd', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
    'viewBox', 'xmlns', 'x', 'y', 'x1', 'y1', 'x2', 'y2',
    'rx', 'ry', 'cx', 'cy', 'r', 'points', 'transform', 'opacity',
    'fill-opacity', 'stroke-opacity', 'clip-path', 'mask', 'marker',
    'font-size', 'font-family', 'text-anchor',
    # ARIA
    'role', 'aria-label', 'aria-hidden', 'aria-expanded', 'aria-controls',
    'aria-describedby', 'aria-labelledby', 'aria-live', 'aria-atomic',
    'aria-required', 'aria-invalid', 'aria-selected', 'aria-checked',
    'aria-disabled', 'aria-readonly', 'aria-multiselectable',
    'aria-valuenow', 'aria-valuemin', 'aria-valuemax', 'aria-valuetext',
    'aria-placeholder', 'aria-autocomplete', 'aria-haspopup',
    'aria-orientation', 'aria-level', 'aria-setsize', 'aria-posinset',
]

# Tags that never have a closing tag in HTML5
VOID_ELEMENTS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
}

# --- Lookup tables built from the lists above ---
TAG_TO_ID   = {tag: i for i, tag in enumerate(TAGS)}
ID_TO_TAG   = {i: tag for i, tag in enumerate(TAGS)}
ATTR_TO_ID  = {attr: i for i, attr in enumerate(ATTRS)}
ID_TO_ATTR  = {i: attr for i, attr in enumerate(ATTRS)}

# Sentinel value: tag or attribute not in the known list
UNKNOWN_ID = 0xFE   # 254 — stored as 1 byte, then followed by a string lookup

# ---------------------------------------------------------------------------
# SHARED VOCABULARY — the "public key"
#
# These strings ship with the encoder and decoder and are NEVER stored
# in the compressed file. HTML using these tokens gets 1-byte or 3-byte
# ID references instead of the full inline string.
#
# Encoding in the content stream:
#   0x00–0xFB (0–251)  : 1-byte string ID  (top 252 most-common tokens)
#   0xFC               : class-list marker (next byte = count, then N token reads)
#   0xFD               : inline string (4-byte length + UTF-8 bytes)
#   0xFE               : None / boolean attribute
#   0xFF HH LL         : 3-byte string ID (IDs 252+)
#
# IDs 0–251 are the "gold tier" — 1 byte each. Order by descending frequency
# so the most common class tokens get the lowest IDs.
# Both encoder and decoder must have this list in identical order — do not reorder.
# ---------------------------------------------------------------------------

SHARED_STRINGS = [
    # ── TIER 1 (IDs 0–251): ultra-common tokens, 1 byte each ──────────────

    # Tailwind: display & layout
    'flex', 'flex-col', 'flex-row', 'flex-wrap', 'flex-1', 'flex-none',
    'flex-grow', 'flex-shrink', 'flex-auto', 'flex-wrap-reverse',
    'grid', 'block', 'inline-block', 'inline', 'inline-flex', 'inline-grid',
    'hidden', 'contents', 'flow-root',

    # Tailwind: position
    'relative', 'absolute', 'fixed', 'sticky', 'static',
    'top-0', 'right-0', 'bottom-0', 'left-0', 'inset-0',
    'top-4', 'right-4', 'inset-x-0', 'inset-y-0',
    'top-1/2', 'left-1/2', '-translate-x-1/2', '-translate-y-1/2',

    # Tailwind: flex & grid alignment
    'items-center', 'items-start', 'items-end', 'items-stretch', 'items-baseline',
    'justify-center', 'justify-between', 'justify-start', 'justify-end',
    'justify-around', 'justify-evenly',
    'self-center', 'self-start', 'self-end', 'self-stretch', 'self-auto',
    'place-items-center', 'place-content-center', 'place-self-center',

    # Tailwind: sizing — width
    'w-full', 'w-auto', 'w-screen', 'w-fit',
    'w-1/2', 'w-1/3', 'w-2/3', 'w-1/4', 'w-3/4',
    'w-4', 'w-5', 'w-6', 'w-8', 'w-10', 'w-12', 'w-16', 'w-24', 'w-32',

    # Tailwind: sizing — height
    'h-full', 'h-auto', 'h-screen', 'h-fit',
    'h-4', 'h-5', 'h-6', 'h-8', 'h-10', 'h-12', 'h-16', 'h-24',

    # Tailwind: max/min sizing
    'max-w-full', 'max-w-xs', 'max-w-sm', 'max-w-md', 'max-w-lg',
    'max-w-xl', 'max-w-2xl', 'max-w-3xl', 'max-w-4xl', 'max-w-5xl',
    'max-w-6xl', 'max-w-7xl', 'max-w-screen-lg', 'max-w-screen-xl',
    'min-h-0', 'min-h-full', 'min-h-screen',

    # Tailwind: padding
    'p-0', 'p-1', 'p-2', 'p-3', 'p-4', 'p-5', 'p-6', 'p-8', 'p-10', 'p-12',
    'px-0', 'px-2', 'px-3', 'px-4', 'px-5', 'px-6', 'px-8', 'px-10',
    'py-0', 'py-1', 'py-2', 'py-3', 'py-4', 'py-5', 'py-6', 'py-8',
    'pt-0', 'pt-2', 'pt-4', 'pt-6', 'pt-8',
    'pb-0', 'pb-2', 'pb-4', 'pb-6', 'pb-8',
    'pl-2', 'pl-4', 'pl-6', 'pr-2', 'pr-4', 'pr-6',

    # Tailwind: margin
    'm-0', 'm-auto', 'mx-auto', 'my-auto',
    'mt-0', 'mt-1', 'mt-2', 'mt-3', 'mt-4', 'mt-5', 'mt-6', 'mt-8', 'mt-10',
    'mb-0', 'mb-1', 'mb-2', 'mb-3', 'mb-4', 'mb-5', 'mb-6', 'mb-8',
    'ml-0', 'ml-2', 'ml-4', 'mr-0', 'mr-2', 'mr-4',
    '-mt-1', '-mt-2', '-mt-4', '-ml-2', '-mr-2',

    # Tailwind: gap
    'gap-0', 'gap-1', 'gap-2', 'gap-3', 'gap-4', 'gap-5', 'gap-6', 'gap-8', 'gap-10',
    'gap-x-2', 'gap-x-4', 'gap-y-2', 'gap-y-4',

    # Tailwind: grid columns
    'grid-cols-1', 'grid-cols-2', 'grid-cols-3', 'grid-cols-4',
    'grid-cols-5', 'grid-cols-6', 'grid-cols-12',
    'col-span-1', 'col-span-2', 'col-span-3', 'col-span-full',

    # Tailwind: space-between
    'space-x-2', 'space-x-4', 'space-y-2', 'space-y-4',

    # Tailwind: typography — sizes
    'text-xs', 'text-sm', 'text-base', 'text-lg', 'text-xl',
    'text-2xl', 'text-3xl', 'text-4xl', 'text-5xl',

    # Tailwind: typography — weight
    'font-thin', 'font-light', 'font-normal', 'font-medium',
    'font-semibold', 'font-bold', 'font-extrabold', 'font-black',

    # Tailwind: typography — misc
    'leading-none', 'leading-tight', 'leading-snug', 'leading-normal',
    'leading-relaxed', 'leading-loose',
    'tracking-tight', 'tracking-normal', 'tracking-wide', 'tracking-wider',
    'text-left', 'text-center', 'text-right', 'text-justify',
    'underline', 'line-through', 'no-underline',
    'uppercase', 'lowercase', 'capitalize', 'normal-case',
    'truncate', 'whitespace-nowrap', 'whitespace-pre', 'break-words', 'break-all',
    'italic', 'not-italic', 'antialiased',

    # Tailwind: text colors (most-used shades)
    'text-white', 'text-black', 'text-current', 'text-transparent',
    'text-gray-400', 'text-gray-500', 'text-gray-600',
    'text-gray-700', 'text-gray-800', 'text-gray-900',
    'text-gray-100', 'text-gray-200', 'text-gray-300',
    'text-red-500', 'text-red-600',
    'text-blue-500', 'text-blue-600',
    'text-green-500', 'text-green-600',
    'text-yellow-500', 'text-amber-500',
    'text-purple-500', 'text-purple-600',
    'text-indigo-500', 'text-indigo-600',
    'text-pink-500', 'text-orange-500',

    # Tailwind: backgrounds (most-used)
    'bg-white', 'bg-black', 'bg-transparent', 'bg-current',
    'bg-gray-50', 'bg-gray-100', 'bg-gray-200', 'bg-gray-700', 'bg-gray-800', 'bg-gray-900',
    'bg-blue-500', 'bg-blue-600',
    'bg-red-500', 'bg-red-600',
    'bg-green-500', 'bg-green-600',
    'bg-purple-500', 'bg-purple-600',
    'bg-indigo-500', 'bg-indigo-600',
    'bg-yellow-500', 'bg-amber-500',
    'bg-orange-500', 'bg-pink-500',

    # Tailwind: borders
    'border', 'border-0', 'border-2', 'border-4',
    'border-t', 'border-b', 'border-l', 'border-r',
    'border-solid', 'border-dashed', 'border-dotted', 'border-none',
    'border-transparent',
    'border-gray-100', 'border-gray-200', 'border-gray-300', 'border-gray-400',
    'border-blue-500', 'border-red-500',

    # Tailwind: border-radius
    'rounded-none', 'rounded-sm', 'rounded', 'rounded-md', 'rounded-lg',
    'rounded-xl', 'rounded-2xl', 'rounded-full',

    # Tailwind: shadows
    'shadow-none', 'shadow-sm', 'shadow', 'shadow-md', 'shadow-lg', 'shadow-xl',

    # Tailwind: opacity / visibility
    'opacity-0', 'opacity-25', 'opacity-50', 'opacity-75', 'opacity-100',
    'visible', 'invisible',

    # Tailwind: overflow
    'overflow-hidden', 'overflow-auto', 'overflow-scroll', 'overflow-visible',
    'overflow-x-hidden', 'overflow-x-auto', 'overflow-y-hidden', 'overflow-y-auto',

    # Tailwind: cursor / pointer / select
    'cursor-auto', 'cursor-default', 'cursor-pointer',
    'cursor-wait', 'cursor-text', 'cursor-move', 'cursor-not-allowed',
    'pointer-events-none', 'pointer-events-auto',
    'select-none', 'select-text', 'select-all',
    'resize', 'resize-none', 'resize-y', 'resize-x',

    # Tailwind: z-index
    'z-0', 'z-10', 'z-20', 'z-30', 'z-40', 'z-50', 'z-auto',

    # Tailwind: transitions
    'transition', 'transition-none', 'transition-all', 'transition-colors',
    'transition-opacity', 'transition-shadow', 'transition-transform',
    'duration-75', 'duration-100', 'duration-150', 'duration-200',
    'duration-300', 'duration-500', 'duration-700',
    'ease-linear', 'ease-in', 'ease-out', 'ease-in-out',
    'delay-100', 'delay-200', 'delay-300',

    # Tailwind: animations
    'animate-none', 'animate-spin', 'animate-ping', 'animate-pulse', 'animate-bounce',

    # ── TIER 2 (IDs 252+): valuable but less frequent, 3 bytes each ────────

    # Tailwind: extended sizing
    'w-0', 'w-1', 'w-2', 'w-3', 'w-20', 'w-48', 'w-64', 'w-96',
    'h-0', 'h-1', 'h-2', 'h-3', 'h-20', 'h-48', 'h-64',
    'w-min', 'w-max', 'h-min', 'h-max',
    'max-h-full', 'max-h-screen', 'min-w-0', 'min-w-full',

    # Tailwind: transforms
    'scale-0', 'scale-50', 'scale-75', 'scale-90', 'scale-95',
    'scale-100', 'scale-105', 'scale-110', 'scale-125',
    'rotate-0', 'rotate-45', 'rotate-90', 'rotate-180',
    '-rotate-45', '-rotate-90', '-rotate-180',
    'translate-x-0', 'translate-y-0',
    'translate-x-full', 'translate-y-full',
    '-translate-x-full', '-translate-y-full',
    'translate-x-1/2', 'translate-y-1/2',
    'skew-x-0', 'skew-y-0',

    # Tailwind: object / aspect
    'object-contain', 'object-cover', 'object-fill', 'object-none', 'object-scale-down',
    'object-top', 'object-center', 'object-bottom',
    'aspect-auto', 'aspect-square', 'aspect-video',

    # Tailwind: divide / ring
    'divide-y', 'divide-x', 'divide-y-2', 'divide-gray-100', 'divide-gray-200',
    'ring', 'ring-0', 'ring-1', 'ring-2', 'ring-4', 'ring-inset',
    'ring-transparent', 'ring-gray-300', 'ring-blue-500',
    'ring-offset-1', 'ring-offset-2', 'ring-offset-4',

    # Tailwind: list / scroll
    'list-none', 'list-disc', 'list-decimal', 'list-inside', 'list-outside',
    'scroll-smooth', 'scroll-auto',
    'overscroll-none', 'overscroll-contain',

    # Tailwind: extended text colors
    'text-gray-50', 'text-blue-400', 'text-blue-700',
    'text-red-400', 'text-red-700', 'text-green-400', 'text-green-700',
    'text-yellow-600', 'text-amber-600',
    'text-purple-400', 'text-purple-700',
    'text-indigo-400', 'text-pink-400', 'text-teal-500', 'text-cyan-500',

    # Tailwind: extended backgrounds
    'bg-gray-300', 'bg-gray-400', 'bg-gray-500', 'bg-gray-600',
    'bg-blue-50', 'bg-blue-100', 'bg-blue-400', 'bg-blue-700',
    'bg-red-50', 'bg-red-100', 'bg-red-400',
    'bg-green-50', 'bg-green-100', 'bg-green-400',
    'bg-yellow-50', 'bg-yellow-100', 'bg-yellow-400',
    'bg-purple-50', 'bg-purple-100', 'bg-purple-400',
    'bg-indigo-50', 'bg-indigo-100',
    'bg-pink-50', 'bg-pink-100',
    'bg-orange-100', 'bg-amber-100',
    'bg-teal-500', 'bg-cyan-500',

    # Tailwind: extended borders / rounded
    'border-8', 'border-t-0', 'border-b-0', 'border-double',
    'border-gray-500', 'border-gray-600',
    'border-blue-300', 'border-red-300', 'border-green-300',
    'border-purple-500', 'border-yellow-500',
    'rounded-3xl', 'rounded-t-lg', 'rounded-b-lg',
    'rounded-l-lg', 'rounded-r-lg', 'rounded-t-none', 'rounded-b-none',
    'rounded-tl-lg', 'rounded-tr-lg', 'rounded-bl-lg', 'rounded-br-lg',

    # Tailwind: extended shadows / opacity
    'shadow-2xl', 'shadow-inner',
    'opacity-5', 'opacity-10', 'opacity-20', 'opacity-30', 'opacity-40',
    'opacity-60', 'opacity-70', 'opacity-80', 'opacity-90', 'opacity-95',

    # Tailwind: extended grid
    'grid-cols-7', 'grid-cols-8', 'grid-cols-10',
    'grid-rows-1', 'grid-rows-2', 'grid-rows-3', 'grid-rows-none',
    'col-span-4', 'col-span-5', 'col-span-6',
    'row-span-1', 'row-span-2', 'row-span-full',
    'col-start-1', 'col-start-2', 'col-end-2', 'col-end-3',
    'auto-cols-auto', 'auto-rows-auto',

    # Tailwind: extended spacing
    'px-1', 'px-12', 'py-10', 'py-12', 'py-16',
    'pt-1', 'pt-3', 'pt-10', 'pb-1', 'pb-3', 'pb-10',
    'pl-0', 'pl-1', 'pl-3', 'pl-8', 'pr-0', 'pr-1', 'pr-3', 'pr-8',
    'm-1', 'm-2', 'm-3', 'm-4', 'm-5',
    'mx-0', 'mx-2', 'mx-4', 'my-0', 'my-2', 'my-4',
    'mt-auto', 'mb-auto', 'ml-auto', 'mr-auto',
    'mt-12', 'mt-16', 'mb-10', 'mb-12', 'mb-16',
    'ml-1', 'ml-3', 'ml-6', 'ml-8', 'mr-1', 'mr-3', 'mr-6', 'mr-8',
    'gap-12', 'gap-16', 'gap-x-6', 'gap-y-6',
    'space-x-0', 'space-x-1', 'space-x-3', 'space-x-6',
    'space-y-0', 'space-y-1', 'space-y-3', 'space-y-6',
    '-mb-2', '-mb-4', '-ml-4', '-mr-4', '-mx-4',

    # Tailwind: focus / hover / active / dark variants
    'focus:outline-none', 'focus:ring-2', 'focus:ring-4',
    'focus:ring-blue-500', 'focus:ring-blue-300', 'focus:ring-offset-2',
    'focus:border-blue-500', 'focus:bg-white', 'focus:text-gray-900',
    'focus-visible:outline-none', 'focus-visible:ring-2',
    'hover:bg-gray-50', 'hover:bg-gray-100', 'hover:bg-gray-200',
    'hover:bg-blue-600', 'hover:bg-blue-700',
    'hover:bg-red-600', 'hover:bg-green-600',
    'hover:text-gray-700', 'hover:text-gray-900', 'hover:text-white',
    'hover:text-blue-500', 'hover:text-blue-600',
    'hover:border-gray-300', 'hover:border-blue-500',
    'hover:shadow', 'hover:shadow-md', 'hover:shadow-lg',
    'hover:opacity-75', 'hover:opacity-90',
    'hover:scale-105', 'hover:scale-110',
    'hover:underline', 'hover:no-underline',
    'active:scale-95', 'active:bg-gray-200', 'active:opacity-75',
    'disabled:opacity-50', 'disabled:cursor-not-allowed',
    'group-hover:block', 'group-hover:flex', 'group-hover:opacity-100',
    'group-hover:text-blue-500', 'group-hover:translate-x-1',

    # Tailwind: dark mode
    'dark:bg-gray-700', 'dark:bg-gray-800', 'dark:bg-gray-900',
    'dark:bg-gray-600', 'dark:bg-transparent',
    'dark:text-white', 'dark:text-gray-100', 'dark:text-gray-200',
    'dark:text-gray-300', 'dark:text-gray-400', 'dark:text-gray-500',
    'dark:border-gray-600', 'dark:border-gray-700',
    'dark:hover:bg-gray-700', 'dark:hover:bg-gray-600',
    'dark:focus:ring-blue-500',

    # Tailwind: responsive variants
    'sm:flex', 'sm:hidden', 'sm:block', 'sm:inline-flex', 'sm:grid',
    'sm:flex-row', 'sm:flex-col', 'sm:flex-wrap',
    'sm:grid-cols-2', 'sm:grid-cols-3',
    'sm:w-full', 'sm:w-1/2', 'sm:w-auto',
    'sm:p-4', 'sm:px-6', 'sm:py-4', 'sm:text-sm', 'sm:text-base',
    'sm:max-w-full', 'sm:items-center', 'sm:justify-between',
    'md:flex', 'md:hidden', 'md:block', 'md:inline-flex', 'md:grid',
    'md:flex-row', 'md:flex-col', 'md:flex-wrap',
    'md:grid-cols-2', 'md:grid-cols-3', 'md:grid-cols-4',
    'md:w-full', 'md:w-1/2', 'md:w-1/3', 'md:w-2/3', 'md:w-auto',
    'md:p-6', 'md:px-8', 'md:py-6', 'md:py-8',
    'md:text-lg', 'md:text-xl', 'md:text-2xl',
    'md:max-w-xl', 'md:max-w-2xl', 'md:max-w-3xl',
    'md:items-center', 'md:justify-between',
    'md:col-span-2', 'md:col-span-3',
    'lg:flex', 'lg:hidden', 'lg:block', 'lg:inline-flex', 'lg:grid',
    'lg:flex-row', 'lg:flex-col',
    'lg:grid-cols-3', 'lg:grid-cols-4', 'lg:grid-cols-5',
    'lg:w-full', 'lg:w-1/2', 'lg:w-1/3', 'lg:w-auto',
    'lg:p-8', 'lg:px-8', 'lg:px-12', 'lg:py-12', 'lg:py-16',
    'lg:text-xl', 'lg:text-2xl', 'lg:text-3xl',
    'lg:max-w-4xl', 'lg:max-w-5xl', 'lg:max-w-7xl',
    'lg:col-span-2', 'lg:col-span-3',
    'xl:flex', 'xl:hidden', 'xl:block', 'xl:grid',
    'xl:grid-cols-4', 'xl:grid-cols-5', 'xl:grid-cols-6',
    'xl:max-w-screen-xl', 'xl:text-3xl', 'xl:text-4xl',
    '2xl:grid-cols-5', '2xl:grid-cols-6', '2xl:max-w-screen-2xl',

    # ── Bootstrap 5 layout ────────────────────────────────────────────────
    'container', 'container-fluid', 'container-sm',
    'container-md', 'container-lg', 'container-xl', 'container-xxl',
    'row', 'col', 'col-auto',
    'col-1', 'col-2', 'col-3', 'col-4', 'col-5', 'col-6',
    'col-7', 'col-8', 'col-9', 'col-10', 'col-11', 'col-12',
    'col-sm-3', 'col-sm-4', 'col-sm-6', 'col-sm-12',
    'col-md-3', 'col-md-4', 'col-md-6', 'col-md-8', 'col-md-12',
    'col-lg-3', 'col-lg-4', 'col-lg-6', 'col-lg-8', 'col-lg-12',
    'col-xl-3', 'col-xl-4', 'col-xl-6',
    'g-0', 'g-1', 'g-2', 'g-3', 'g-4', 'g-5',
    'gx-2', 'gx-3', 'gx-4', 'gy-2', 'gy-3', 'gy-4',

    # Bootstrap 5: buttons
    'btn', 'btn-primary', 'btn-secondary', 'btn-success',
    'btn-danger', 'btn-warning', 'btn-info', 'btn-light', 'btn-dark', 'btn-link',
    'btn-sm', 'btn-lg',
    'btn-outline-primary', 'btn-outline-secondary',
    'btn-outline-success', 'btn-outline-danger',
    'btn-outline-warning', 'btn-outline-info',
    'btn-close',

    # Bootstrap 5: nav
    'nav', 'navbar', 'navbar-nav',
    'navbar-expand', 'navbar-expand-sm', 'navbar-expand-md',
    'navbar-expand-lg', 'navbar-expand-xl',
    'navbar-light', 'navbar-dark', 'navbar-brand',
    'nav-link', 'nav-item', 'nav-tabs', 'nav-pills',
    'nav-fill', 'nav-justified', 'nav-underline',
    'navbar-toggler', 'navbar-toggler-icon', 'navbar-collapse', 'navbar-text',
    'dropdown-toggle', 'dropdown-menu', 'dropdown-item',
    'dropdown-divider', 'dropdown-header',
    'dropup', 'dropend', 'dropstart',

    # Bootstrap 5: cards
    'card', 'card-body', 'card-title', 'card-subtitle', 'card-text',
    'card-header', 'card-footer', 'card-img-top', 'card-img-bottom',
    'card-img-overlay', 'card-group',

    # Bootstrap 5: forms
    'form-control', 'form-control-sm', 'form-control-lg',
    'form-group', 'form-label', 'form-text',
    'form-check', 'form-check-input', 'form-check-label', 'form-check-inline',
    'form-select', 'form-select-sm', 'form-select-lg',
    'form-range', 'form-floating',
    'input-group', 'input-group-text', 'input-group-sm', 'input-group-lg',
    'was-validated', 'is-valid', 'is-invalid',
    'valid-feedback', 'invalid-feedback', 'valid-tooltip', 'invalid-tooltip',

    # Bootstrap 5: modal
    'modal', 'modal-dialog', 'modal-content',
    'modal-header', 'modal-body', 'modal-footer', 'modal-title',
    'modal-sm', 'modal-lg', 'modal-xl', 'modal-fullscreen',

    # Bootstrap 5: display utilities
    'd-flex', 'd-none', 'd-block', 'd-inline',
    'd-inline-flex', 'd-inline-block', 'd-grid',
    'd-sm-flex', 'd-sm-none', 'd-sm-block',
    'd-md-flex', 'd-md-none', 'd-md-block',
    'd-lg-flex', 'd-lg-none', 'd-lg-block',

    # Bootstrap 5: flex utilities (Bootstrap-specific names)
    'align-items-center', 'align-items-start', 'align-items-end',
    'align-items-baseline', 'align-items-stretch',
    'align-self-center', 'align-self-start', 'align-self-end',
    'justify-content-center', 'justify-content-between',
    'justify-content-start', 'justify-content-end',
    'justify-content-around', 'justify-content-evenly',
    'flex-column', 'flex-row-reverse', 'flex-column-reverse',
    'flex-grow-0', 'flex-grow-1', 'flex-shrink-0', 'flex-shrink-1', 'flex-fill',
    'order-first', 'order-last', 'order-0', 'order-1', 'order-2', 'order-3',

    # Bootstrap 5: text utilities
    'text-start', 'text-end', 'text-muted', 'text-decoration-none',
    'text-decoration-underline', 'text-truncate', 'text-break',
    'text-primary', 'text-secondary', 'text-success',
    'text-danger', 'text-warning', 'text-info',
    'text-light', 'text-dark', 'text-body', 'text-white-50',
    'text-reset', 'text-opacity-75', 'text-opacity-50',
    'fw-bold', 'fw-bolder', 'fw-semibold', 'fw-normal', 'fw-light', 'fw-lighter',
    'fst-italic', 'fst-normal',
    'lh-base', 'lh-sm', 'lh-lg', 'lh-1',
    'fs-1', 'fs-2', 'fs-3', 'fs-4', 'fs-5', 'fs-6',

    # Bootstrap 5: Bootstrap margin/padding additions (ms/me are Bootstrap-specific)
    'ms-0', 'ms-1', 'ms-2', 'ms-3', 'ms-4', 'ms-5', 'ms-auto',
    'me-0', 'me-1', 'me-2', 'me-3', 'me-4', 'me-5', 'me-auto',
    'mx-1', 'mx-3', 'mx-5',
    'my-1', 'my-3', 'my-5',

    # Bootstrap 5: background utilities
    'bg-primary', 'bg-secondary', 'bg-success', 'bg-danger',
    'bg-warning', 'bg-info', 'bg-light', 'bg-dark',
    'bg-body', 'bg-gradient',
    'bg-opacity-10', 'bg-opacity-25', 'bg-opacity-50', 'bg-opacity-75',

    # Bootstrap 5: border utilities
    'border-primary', 'border-secondary', 'border-success',
    'border-danger', 'border-warning', 'border-info',
    'border-light', 'border-dark', 'border-white',
    'border-top', 'border-bottom', 'border-start', 'border-end',
    'border-1', 'border-3', 'border-5',
    'border-opacity-10', 'border-opacity-25', 'border-opacity-50',

    # Bootstrap 5: rounded
    'rounded-circle', 'rounded-pill',
    'rounded-top', 'rounded-bottom', 'rounded-start', 'rounded-end',

    # Bootstrap 5: position
    'position-relative', 'position-absolute', 'position-fixed',
    'position-sticky', 'position-static',
    'start-0', 'end-0',
    'translate-middle', 'translate-middle-x', 'translate-middle-y',

    # Bootstrap 5: sizing
    'w-25', 'w-50', 'w-75', 'w-100',
    'h-25', 'h-50', 'h-75', 'h-100',
    'mw-100', 'mh-100', 'vw-100', 'vh-100',
    'min-vw-100', 'min-vh-100',
    'img-fluid', 'img-thumbnail',

    # Bootstrap 5: misc utilities
    'fade', 'show', 'active', 'disabled', 'collapse', 'collapsing',
    'stretched-link', 'pe-none', 'pe-auto',
    'user-select-none', 'user-select-all', 'user-select-auto',
    'visually-hidden', 'visually-hidden-focusable', 'sr-only',

    # Bootstrap 5: components
    'badge', 'alert', 'alert-primary', 'alert-secondary', 'alert-success',
    'alert-danger', 'alert-warning', 'alert-info', 'alert-light', 'alert-dark',
    'alert-dismissible', 'alert-heading', 'alert-link',
    'spinner-border', 'spinner-grow', 'spinner-border-sm', 'spinner-grow-sm',
    'breadcrumb', 'breadcrumb-item',
    'pagination', 'page-item', 'page-link',
    'progress', 'progress-bar', 'progress-bar-striped', 'progress-bar-animated',
    'list-group', 'list-group-item', 'list-group-item-action',
    'list-group-flush', 'list-group-horizontal',
    'list-unstyled', 'list-inline', 'list-inline-item',
    'table', 'table-striped', 'table-bordered', 'table-hover',
    'table-sm', 'table-responsive', 'table-dark', 'table-light',
    'accordion', 'accordion-item', 'accordion-header',
    'accordion-button', 'accordion-body', 'accordion-flush',
    'tab-content', 'tab-pane',
    'toast', 'toast-header', 'toast-body', 'toast-container',
    'offcanvas', 'offcanvas-header', 'offcanvas-body', 'offcanvas-title',
    'offcanvas-start', 'offcanvas-end', 'offcanvas-top', 'offcanvas-bottom',
    'tooltip', 'tooltip-inner', 'popover', 'popover-header', 'popover-body',
    'carousel', 'carousel-item', 'carousel-inner', 'carousel-indicators',

    # ── Common semantic / custom class names ──────────────────────────────
    'wrapper', 'inner', 'outer', 'content', 'main-content',
    'page', 'section', 'header', 'footer', 'sidebar',
    'hero', 'banner', 'panel', 'widget', 'box',
    'menu', 'submenu', 'item', 'link', 'icon', 'logo',
    'title', 'subtitle', 'heading', 'description', 'caption',
    'label', 'chip', 'pill', 'tag',
    'overlay', 'backdrop', 'drawer', 'popup',
    'avatar', 'thumbnail', 'cover',
    'cta', 'divider', 'separator',
    'error', 'success', 'warning', 'info',
    'primary', 'secondary', 'accent', 'muted',
    'loading', 'skeleton', 'spinner',

    # ── Common attribute values ────────────────────────────────────────────
    'text/css', 'text/javascript', 'application/json',
    'text/html; charset=utf-8', 'UTF-8', 'utf-8',
    'stylesheet', 'preload', 'prefetch', 'preconnect', 'modulepreload',
    'font', 'fetch', 'image', 'worker',
    '_blank', '_self', '_parent', '_top',
    'noopener noreferrer', 'noopener', 'noreferrer', 'nofollow',
    'no-referrer', 'strict-origin-when-cross-origin',
    'anonymous', 'use-credentials',
    'get', 'post', 'GET', 'POST',
    'checkbox', 'radio', 'text', 'email', 'password',
    'submit', 'button', 'reset',
    'number', 'tel', 'url', 'search', 'date', 'file',
    'color', 'range', 'time', 'month', 'week', 'datetime-local',
    'text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data',
    'lazy', 'eager',
    'ltr', 'rtl', 'auto',
    'true', 'false', 'yes', 'no', 'on', 'off',
    'en', 'en-US', 'en-GB',

    # ── ARIA role values ───────────────────────────────────────────────────
    # (button, checkbox, radio, hidden, grid, row, menu, alert, search,
    #  banner, heading, separator, tooltip already appear earlier in the list)
    'none', 'presentation',
    'navigation', 'main', 'contentinfo',
    'dialog', 'alertdialog', 'status', 'log',
    'region', 'form',
    'menuitem', 'menuitemcheckbox', 'menuitemradio', 'menubar',
    'tab', 'tablist', 'tabpanel',
    'listbox', 'option', 'combobox',
    'toolbar', 'progressbar', 'slider', 'spinbutton', 'switch',
    'treeitem', 'tree', 'treegrid',
    'rowheader', 'columnheader', 'rowgroup', 'cell', 'gridcell',
    'article', 'complementary', 'list', 'listitem',
    'figure', 'term', 'definition',
    'note', 'document', 'application',

    # ── Wikipedia / MediaWiki ─────────────────────────────────────────────
    'mw-parser-output', 'mw-editsection', 'mw-headline',
    'mw-redirect', 'mw-content-text', 'mw-page-title-main',
    'mw-body', 'mw-body-content', 'mw-content-ltr', 'mw-content-rtl',
    'reference', 'references', 'reflist', 'citation', 'cite_note', 'cite_ref',
    'wikitable', 'sortable', 'navbox', 'navbox-inner',
    'navbox-group', 'navbox-list', 'navbox-odd', 'navbox-even',
    'infobox', 'tocnumber', 'toctitle', 'toc',
    'hatnote', 'thumb', 'thumbinner', 'thumbcaption',
    'catlinks', 'mw-normal-catlinks',
]

# Verify no duplicates (catches bugs during development)
assert len(SHARED_STRINGS) == len(set(SHARED_STRINGS)), \
    f"Duplicate entries in SHARED_STRINGS! " \
    f"Count: {len(SHARED_STRINGS)}, Unique: {len(set(SHARED_STRINGS))}"

# Both encoder and decoder access this as an ordered list.
# ID 0 = SHARED_STRINGS[0], ID 1 = SHARED_STRINGS[1], etc.
SHARED_STR_TO_ID = {s: i for i, s in enumerate(SHARED_STRINGS)}
SHARED_COUNT     = len(SHARED_STRINGS)
