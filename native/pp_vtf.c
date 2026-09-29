/* VTF texture decoding (formats used by particle materials) and sprite sheets. */
#include "pp_internal.h"

enum {
    IMG_RGBA8888 = 0,
    IMG_ABGR8888,
    IMG_RGB888,
    IMG_BGR888,
    IMG_RGB565,
    IMG_I8,
    IMG_IA88,
    IMG_P8,
    IMG_A8,
    IMG_RGB888_BLUESCREEN,
    IMG_BGR888_BLUESCREEN,
    IMG_ARGB8888,
    IMG_BGRA8888,
    IMG_DXT1,
    IMG_DXT3,
    IMG_DXT5,
    IMG_BGRX8888,
    IMG_BGR565,
    IMG_BGRX5551,
    IMG_BGRA4444,
    IMG_DXT1_ONEBITALPHA,
    IMG_BGRA5551,
    IMG_UV88,
    IMG_UVWQ8888,
    IMG_RGBA16161616F,
    IMG_RGBA16161616,
    IMG_UVLX8888
};

static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static float rdf(const uint8_t *p) {
    uint32_t u = rd32(p);
    float f;
    memcpy(&f, &u, 4);
    return finitef(f) ? f : 0.0f;
}

/* bytes for one w*h image, -1 if the format is unsupported */
static long long image_bytes(int fmt, int w, int h) {
    long long bw = (w + 3) / 4, bh = (h + 3) / 4, px = (long long)w * h;
    switch (fmt) {
    case IMG_DXT1:
    case IMG_DXT1_ONEBITALPHA:
        return bw * bh * 8;
    case IMG_DXT3:
    case IMG_DXT5:
        return bw * bh * 16;
    case IMG_RGBA8888:
    case IMG_ABGR8888:
    case IMG_ARGB8888:
    case IMG_BGRA8888:
    case IMG_BGRX8888:
    case IMG_UVWQ8888:
    case IMG_UVLX8888:
        return px * 4;
    case IMG_RGB888:
    case IMG_BGR888:
    case IMG_RGB888_BLUESCREEN:
    case IMG_BGR888_BLUESCREEN:
        return px * 3;
    case IMG_RGB565:
    case IMG_BGR565:
    case IMG_IA88:
    case IMG_BGRX5551:
    case IMG_BGRA4444:
    case IMG_BGRA5551:
    case IMG_UV88:
        return px * 2;
    case IMG_I8:
    case IMG_A8:
    case IMG_P8:
        return px;
    case IMG_RGBA16161616F:
    case IMG_RGBA16161616:
        return px * 8;
    default:
        return -1;
    }
}

static void c565(uint16_t c, uint8_t *o) {
    int r = (c >> 11) & 31, g = (c >> 5) & 63, b = c & 31;
    o[0] = (uint8_t)((r * 527 + 23) >> 6);
    o[1] = (uint8_t)((g * 259 + 33) >> 6);
    o[2] = (uint8_t)((b * 527 + 23) >> 6);
}

static void dxt_colors(const uint8_t *b, uint8_t rgba[16][4], int allow_alpha) {
    uint8_t c[4][4];
    uint16_t c0 = rd16(b), c1 = rd16(b + 2);
    uint32_t bits = rd32(b + 4);
    int i, k;
    c565(c0, c[0]);
    c565(c1, c[1]);
    c[0][3] = c[1][3] = 255;
    if (c0 > c1 || !allow_alpha) {
        for (k = 0; k < 3; k++) {
            c[2][k] = (uint8_t)((2 * c[0][k] + c[1][k] + 1) / 3);
            c[3][k] = (uint8_t)((c[0][k] + 2 * c[1][k] + 1) / 3);
        }
        c[2][3] = c[3][3] = 255;
    } else {
        for (k = 0; k < 3; k++) {
            c[2][k] = (uint8_t)((c[0][k] + c[1][k]) / 2);
            c[3][k] = 0;
        }
        c[2][3] = 255;
        c[3][3] = 0;
    }
    for (i = 0; i < 16; i++) memcpy(rgba[i], c[(bits >> (2 * i)) & 3], 4);
}

static void dxt5_alpha(const uint8_t *b, uint8_t rgba[16][4]) {
    uint8_t a[8];
    uint64_t bits = 0;
    int i;
    a[0] = b[0];
    a[1] = b[1];
    if (a[0] > a[1]) {
        for (i = 1; i < 7; i++) a[i + 1] = (uint8_t)(((7 - i) * a[0] + i * a[1] + 3) / 7);
    } else {
        for (i = 1; i < 5; i++) a[i + 1] = (uint8_t)(((5 - i) * a[0] + i * a[1] + 2) / 5);
        a[6] = 0;
        a[7] = 255;
    }
    for (i = 0; i < 6; i++) bits |= (uint64_t)b[2 + i] << (8 * i);
    for (i = 0; i < 16; i++) rgba[i][3] = a[(bits >> (3 * i)) & 7];
}

static float half_to_float(uint16_t h) {
    int s = (h >> 15) & 1, e = (h >> 10) & 31, m = h & 1023;
    float v;
    if (e == 0)
        v = (float)m * (1.0f / 16777216.0f);
    else if (e == 31)
        v = m ? 0.0f : 65504.0f;
    else
        v = ldexpf((float)(m + 1024), e - 25);
    return s ? -v : v;
}

/* decode one image to straight RGBA8 */
static int decode_image(int fmt, const uint8_t *src, int w, int h, uint8_t *dst) {
    int x, y, i;
    long long px = (long long)w * h;
    if (fmt == IMG_DXT1 || fmt == IMG_DXT1_ONEBITALPHA || fmt == IMG_DXT3 || fmt == IMG_DXT5) {
        int bw = (w + 3) / 4, bh = (h + 3) / 4, bx, by, block = (fmt == IMG_DXT3 || fmt == IMG_DXT5) ? 16 : 8;
        for (by = 0; by < bh; by++) {
            for (bx = 0; bx < bw; bx++) {
                const uint8_t *b = src + ((long long)by * bw + bx) * block;
                uint8_t rgba[16][4];
                if (block == 8) {
                    dxt_colors(b, rgba, 1);
                } else {
                    dxt_colors(b + 8, rgba, 0);
                    if (fmt == IMG_DXT5) {
                        dxt5_alpha(b, rgba);
                    } else {
                        for (i = 0; i < 16; i++) {
                            int nib = (b[i / 2] >> (4 * (i & 1))) & 15;
                            rgba[i][3] = (uint8_t)(nib * 17);
                        }
                    }
                }
                for (i = 0; i < 16; i++) {
                    int px_x = bx * 4 + (i & 3), px_y = by * 4 + (i >> 2);
                    if (px_x < w && px_y < h) memcpy(dst + ((long long)px_y * w + px_x) * 4, rgba[i], 4);
                }
            }
        }
        return 1;
    }
    for (i = 0; i < px; i++) {
        uint8_t *o = dst + (long long)i * 4;
        const uint8_t *s;
        uint16_t v;
        switch (fmt) {
        case IMG_RGBA8888:
        case IMG_UVWQ8888:
        case IMG_UVLX8888:
            memcpy(o, src + (long long)i * 4, 4);
            break;
        case IMG_ABGR8888:
            s = src + (long long)i * 4;
            o[0] = s[3], o[1] = s[2], o[2] = s[1], o[3] = s[0];
            break;
        case IMG_ARGB8888:
            s = src + (long long)i * 4;
            o[0] = s[1], o[1] = s[2], o[2] = s[3], o[3] = s[0];
            break;
        case IMG_BGRA8888:
            s = src + (long long)i * 4;
            o[0] = s[2], o[1] = s[1], o[2] = s[0], o[3] = s[3];
            break;
        case IMG_BGRX8888:
            s = src + (long long)i * 4;
            o[0] = s[2], o[1] = s[1], o[2] = s[0], o[3] = 255;
            break;
        case IMG_RGB888:
        case IMG_RGB888_BLUESCREEN:
            s = src + (long long)i * 3;
            o[0] = s[0], o[1] = s[1], o[2] = s[2], o[3] = 255;
            if (fmt == IMG_RGB888_BLUESCREEN && s[0] == 0 && s[1] == 0 && s[2] == 255) o[3] = 0;
            break;
        case IMG_BGR888:
        case IMG_BGR888_BLUESCREEN:
            s = src + (long long)i * 3;
            o[0] = s[2], o[1] = s[1], o[2] = s[0], o[3] = 255;
            if (fmt == IMG_BGR888_BLUESCREEN && s[0] == 255 && s[1] == 0 && s[2] == 0) o[3] = 0;
            break;
        case IMG_RGB565:
            c565(rd16(src + (long long)i * 2), o);
            {
                uint8_t t = o[0];
                o[0] = o[2];
                o[2] = t;
            }
            o[3] = 255;
            break;
        case IMG_BGR565:
            c565(rd16(src + (long long)i * 2), o);
            o[3] = 255;
            break;
        case IMG_BGRX5551:
        case IMG_BGRA5551:
            v = rd16(src + (long long)i * 2);
            o[0] = (uint8_t)(((v >> 10) & 31) * 255 / 31);
            o[1] = (uint8_t)(((v >> 5) & 31) * 255 / 31);
            o[2] = (uint8_t)((v & 31) * 255 / 31);
            o[3] = (fmt == IMG_BGRX5551 || (v & 0x8000)) ? 255 : 0;
            break;
        case IMG_BGRA4444:
            v = rd16(src + (long long)i * 2);
            o[0] = (uint8_t)(((v >> 8) & 15) * 17);
            o[1] = (uint8_t)(((v >> 4) & 15) * 17);
            o[2] = (uint8_t)((v & 15) * 17);
            o[3] = (uint8_t)(((v >> 12) & 15) * 17);
            break;
        case IMG_I8:
            o[0] = o[1] = o[2] = src[i], o[3] = 255;
            break;
        case IMG_IA88:
            o[0] = o[1] = o[2] = src[(long long)i * 2], o[3] = src[(long long)i * 2 + 1];
            break;
        case IMG_A8:
            o[0] = o[1] = o[2] = 255, o[3] = src[i];
            break;
        case IMG_UV88:
            o[0] = src[(long long)i * 2], o[1] = src[(long long)i * 2 + 1], o[2] = 0, o[3] = 255;
            break;
        case IMG_RGBA16161616F: {
            int k;
            for (k = 0; k < 4; k++)
                o[k] = (uint8_t)(saturatef(half_to_float(rd16(src + (long long)i * 8 + k * 2))) * 255.0f + 0.5f);
            break;
        }
        case IMG_RGBA16161616: {
            int k;
            for (k = 0; k < 4; k++) o[k] = src[(long long)i * 8 + k * 2 + 1];
            break;
        }
        default:
            return 0;
        }
    }
    (void)x;
    (void)y;
    return 1;
}

static void premultiply(uint8_t *p, long long px) {
    long long i;
    for (i = 0; i < px; i++, p += 4) {
        unsigned a = p[3];
        p[0] = (uint8_t)((p[0] * a + 127) / 255);
        p[1] = (uint8_t)((p[1] * a + 127) / 255);
        p[2] = (uint8_t)((p[2] * a + 127) / 255);
    }
}

/* extend the chain down to 1x1 with a box filter */
static int build_missing_mips(Material *m) {
    while (m->mips < PP_MAX_MIPS) {
        int pw = m->mip_w[m->mips - 1], ph = m->mip_h[m->mips - 1], w, h, x, y, k;
        const uint8_t *src = m->mip_data[m->mips - 1];
        uint8_t *dst;
        if (pw <= 1 && ph <= 1) break;
        w = pw > 1 ? pw / 2 : 1;
        h = ph > 1 ? ph / 2 : 1;
        dst = (uint8_t *)malloc((size_t)w * h * 4);
        if (!dst) return 0;
        for (y = 0; y < h; y++) {
            int y0 = y * 2 < ph ? y * 2 : ph - 1, y1 = y * 2 + 1 < ph ? y * 2 + 1 : ph - 1;
            for (x = 0; x < w; x++) {
                int x0 = x * 2 < pw ? x * 2 : pw - 1, x1 = x * 2 + 1 < pw ? x * 2 + 1 : pw - 1;
                for (k = 0; k < 4; k++) {
                    unsigned s = src[((size_t)y0 * pw + x0) * 4 + k] + src[((size_t)y0 * pw + x1) * 4 + k] +
                                 src[((size_t)y1 * pw + x0) * 4 + k] + src[((size_t)y1 * pw + x1) * 4 + k];
                    dst[((size_t)y * w + x) * 4 + k] = (uint8_t)((s + 2) / 4);
                }
            }
        }
        m->mip_w[m->mips] = w;
        m->mip_h[m->mips] = h;
        m->mip_data[m->mips] = dst;
        m->mips++;
    }
    return 1;
}

void pp_material_release(Material *m) {
    int i;
    if (!m) return;
    for (i = 0; i < m->mips; i++) free(m->mip_data[i]);
    if (m->seqs) {
        for (i = 0; i < m->nseq; i++) free(m->seqs[i].frames);
        free(m->seqs);
    }
    memset(m, 0, sizeof(*m));
}

static int parse_sheet(const uint8_t *d, long long len, Material *m) {
    uint32_t version, nseq, s;
    long long pos = 8;
    int max_seq = -1;
    if (len < 8) return 0;
    version = rd32(d);
    nseq = rd32(d + 4);
    if (version > 1 || nseq > PP_MAX_SEQUENCES) return 0;
    m->seqs = (SheetSeq *)calloc(PP_MAX_SEQUENCES, sizeof(SheetSeq));
    if (!m->seqs) return 0;
    m->nseq = PP_MAX_SEQUENCES;
    for (s = 0; s < nseq; s++) {
        uint32_t num, clamp, frames, f;
        int coords = version == 1 ? 4 : 1;
        SheetSeq *q;
        if (pos + 16 > len) return 0;
        num = rd32(d + pos);
        clamp = rd32(d + pos + 4);
        frames = rd32(d + pos + 8);
        if (num >= PP_MAX_SEQUENCES || frames > 4096) return 0;
        q = &m->seqs[num];
        free(q->frames);
        q->frames = NULL;
        q->count = 0;
        q->clamp = clamp != 0;
        q->duration = rdf(d + pos + 12);
        pos += 16;
        if (pos + (long long)frames * (4 + 16 * coords) > len) return 0;
        q->frames = frames ? (SheetFrame *)calloc(frames, sizeof(SheetFrame)) : NULL;
        if (frames && !q->frames) return 0;
        for (f = 0; f < frames; f++) {
            SheetFrame *fr = &q->frames[f];
            fr->duration = rdf(d + pos);
            fr->u0 = rdf(d + pos + 4);
            fr->v0 = rdf(d + pos + 8);
            fr->u1 = rdf(d + pos + 12);
            fr->v1 = rdf(d + pos + 16);
            pos += 4 + 16 * coords;
        }
        q->count = (int)frames;
        if ((int)num > max_seq) max_seq = (int)num;
    }
    m->nseq = max_seq + 1;
    return 1;
}

int pp_vtf_decode(const uint8_t *d, int size, Material *m) {
    uint32_t major, minor, header_size, flags;
    int width, height, frames, first_frame, fmt, mips, lo_fmt, lo_w, lo_h, depth = 1, faces = 1;
    long long hi_off = -1, sheet_off = -1, offsets[PP_MAX_MIPS * 2];
    int level, base = -1, out = 0;
    memset(m, 0, sizeof(*m));
    if (!d || size < 64 || memcmp(d, "VTF\0", 4) != 0) {
        pp_set_error("not a VTF file");
        return 0;
    }
    major = rd32(d + 4);
    minor = rd32(d + 8);
    header_size = rd32(d + 12);
    if (major != 7 || minor > 5) {
        pp_set_error("unsupported VTF version %u.%u", major, minor);
        return 0;
    }
    width = rd16(d + 16);
    height = rd16(d + 18);
    flags = rd32(d + 20);
    frames = rd16(d + 24);
    first_frame = rd16(d + 26);
    fmt = (int)rd32(d + 52);
    mips = d[56];
    lo_fmt = (int)rd32(d + 57);
    lo_w = d[61];
    lo_h = d[62];
    if (minor >= 2 && size >= 65) depth = rd16(d + 63);
    if (depth < 1) depth = 1;
    if (frames < 1) frames = 1;
    if (mips < 1) mips = 1;
    if (mips > 24) mips = 24;
    if (flags & 0x4000) faces = (minor < 5 && first_frame != 0xffff) ? 7 : 6;
    if (width < 1 || height < 1 || image_bytes(fmt, 1, 1) < 0) {
        pp_set_error("unsupported VTF image format %d (%dx%d)", fmt, width, height);
        return 0;
    }
    if (minor >= 3) {
        uint32_t nres, r;
        if (size < 80) {
            pp_set_error("truncated VTF header");
            return 0;
        }
        nres = rd32(d + 68);
        if (nres > 32 || 80 + (long long)nres * 8 > size) {
            pp_set_error("bad VTF resource table");
            return 0;
        }
        for (r = 0; r < nres; r++) {
            const uint8_t *e = d + 80 + r * 8;
            if (e[0] == 0x30 && e[1] == 0 && e[2] == 0) hi_off = rd32(e + 4);
            if (e[0] == 0x10 && e[1] == 0 && e[2] == 0 && !(e[3] & 2)) sheet_off = rd32(e + 4);
        }
    } else {
        long long lo = (lo_fmt >= 0 && lo_w > 0 && lo_h > 0) ? image_bytes(lo_fmt, lo_w, lo_h) : 0;
        hi_off = (long long)header_size + (lo > 0 ? lo : 0);
    }
    if (hi_off < 0 || hi_off > size) {
        pp_set_error("VTF image data not found");
        return 0;
    }
    /* data is stored from the smallest mip to the largest */
    {
        long long off = hi_off;
        int overflow = 0;
        for (level = mips - 1; level >= 0; level--) {
            int w = width >> level, h = height >> level, dd = depth >> level;
            long long step;
            if (w < 1) w = 1;
            if (h < 1) h = 1;
            if (dd < 1) dd = 1;
            if (overflow || off > size) {
                offsets[level] = -1;
                overflow = 1;
                continue;
            }
            offsets[level] = off;
            /* multiply step by step so hostile counts cannot overflow */
            step = image_bytes(fmt, w, h);
            if (step > size || (step *= dd) > size || (step *= frames) > size || (step *= faces) > size) {
                overflow = 1;
                continue;
            }
            off += step;
        }
    }
    for (level = 0; level < mips && level < PP_MAX_MIPS * 2; level++) {
        int w = width >> level, h = height >> level;
        if (w < 1) w = 1;
        if (h < 1) h = 1;
        if (w <= PP_MAX_TEXTURE_DIM && h <= PP_MAX_TEXTURE_DIM) {
            base = level;
            break;
        }
    }
    if (base < 0) {
        pp_set_error("VTF has no mip small enough (%dx%d)", width, height);
        return 0;
    }
    for (level = base; level < mips && level < PP_MAX_MIPS * 2 && out < PP_MAX_MIPS; level++) {
        int w = width >> level, h = height >> level;
        long long bytes;
        uint8_t *px;
        if (w < 1) w = 1;
        if (h < 1) h = 1;
        bytes = image_bytes(fmt, w, h);
        if (offsets[level] < 0 || bytes > size || offsets[level] + bytes > size) {
            if (out == 0) {
                pp_set_error("truncated VTF image data");
                pp_material_release(m);
                return 0;
            }
            break;
        }
        px = (uint8_t *)malloc((size_t)w * h * 4);
        if (!px || !decode_image(fmt, d + offsets[level], w, h, px)) {
            free(px);
            pp_set_error("cannot decode VTF format %d", fmt);
            pp_material_release(m);
            return 0;
        }
        premultiply(px, (long long)w * h);
        m->mip_w[out] = w;
        m->mip_h[out] = h;
        m->mip_data[out] = px;
        out++;
    }
    m->mips = out;
    m->width = m->mip_w[0];
    m->height = m->mip_h[0];
    if (!build_missing_mips(m)) {
        pp_material_release(m);
        pp_set_error("out of memory");
        return 0;
    }
    if (sheet_off >= 0 && sheet_off + 4 <= size) {
        long long len = rd32(d + sheet_off);
        if (sheet_off + 4 + len <= size && !parse_sheet(d + sheet_off + 4, len, m)) {
            int i;
            if (m->seqs) {
                for (i = 0; i < PP_MAX_SEQUENCES; i++) free(m->seqs[i].frames);
                free(m->seqs);
            }
            m->seqs = NULL;
            m->nseq = 0;
        }
    }
    return 1;
}

int pp_material_from_rgba(const uint8_t *rgba, int w, int h, Material *m) {
    memset(m, 0, sizeof(*m));
    if (!rgba || w < 1 || h < 1 || w > PP_MAX_TEXTURE_DIM * 4 || h > PP_MAX_TEXTURE_DIM * 4) {
        pp_set_error("bad RGBA image size %dx%d", w, h);
        return 0;
    }
    m->mip_data[0] = (uint8_t *)malloc((size_t)w * h * 4);
    if (!m->mip_data[0]) {
        pp_set_error("out of memory");
        return 0;
    }
    memcpy(m->mip_data[0], rgba, (size_t)w * h * 4);
    premultiply(m->mip_data[0], (long long)w * h);
    m->mip_w[0] = m->width = w;
    m->mip_h[0] = m->height = h;
    m->mips = 1;
    if (!build_missing_mips(m)) {
        pp_material_release(m);
        pp_set_error("out of memory");
        return 0;
    }
    return 1;
}

const Material *pp_default_material(void) {
    static Material mat;
    static int ready = 0;
    if (!ready) {
        static uint8_t px[64 * 64 * 4];
        int x, y;
        for (y = 0; y < 64; y++) {
            for (x = 0; x < 64; x++) {
                float dx = (x + 0.5f) / 32.0f - 1.0f, dy = (y + 0.5f) / 32.0f - 1.0f;
                float a = saturatef(1.0f - sqrtf(dx * dx + dy * dy));
                uint8_t *p = px + (y * 64 + x) * 4;
                a = a * a;
                p[0] = p[1] = p[2] = 255;
                p[3] = (uint8_t)(a * 255.0f + 0.5f);
            }
        }
        if (pp_material_from_rgba(px, 64, 64, &mat)) ready = 1;
    }
    return ready ? &mat : NULL;
}

int pp_sheet_frame_count(const Material *m, int seq) {
    if (!m || !m->seqs || m->nseq <= 0) return 0;
    if (seq < 0 || seq >= m->nseq || m->seqs[seq].count <= 0) seq = 0;
    return seq < m->nseq ? m->seqs[seq].count : 0;
}

float pp_sheet_frames(const Material *m, int seq, float phase, const SheetFrame **a, const SheetFrame **b) {
    const SheetSeq *q;
    float total = 0.0f, t, acc = 0.0f;
    int i;
    *a = *b = NULL;
    if (!m || !m->seqs || m->nseq <= 0) return 0.0f;
    if (seq < 0 || seq >= m->nseq || m->seqs[seq].count <= 0) {
        for (seq = 0; seq < m->nseq && m->seqs[seq].count <= 0; seq++) {
        }
        if (seq >= m->nseq) return 0.0f;
    }
    q = &m->seqs[seq];
    for (i = 0; i < q->count; i++) total += q->frames[i].duration > 0 ? q->frames[i].duration : 0.0f;
    if (!(total > 1e-6f) || !finitef(phase)) {
        *a = *b = &q->frames[0];
        return 0.0f;
    }
    t = phase * total;
    if (q->clamp) {
        t = clampf(t, 0.0f, total * 0.99999f);
    } else {
        t = fmodf(t, total);
        if (t < 0) t += total;
    }
    for (i = 0; i < q->count; i++) {
        float dur = q->frames[i].duration > 0 ? q->frames[i].duration : 0.0f;
        if (t < acc + dur || i == q->count - 1) {
            int next = i + 1 < q->count ? i + 1 : (q->clamp ? i : 0);
            *a = &q->frames[i];
            *b = &q->frames[next];
            return dur > 0 ? saturatef((t - acc) / dur) : 0.0f;
        }
        acc += dur;
    }
    *a = *b = &q->frames[0];
    return 0.0f;
}
