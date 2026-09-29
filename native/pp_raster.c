/* Software renderer: background, grid, sprites, trails and ropes. */
#if !defined(_WIN32) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200112L /* pthreads and sysconf; not _GNU_SOURCE, whose math.h clashes with finitef */
#endif
#include "pp_internal.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

/* Sprite pixels drawn per frame, shared by all systems, before particles are thinned out:
   OVERDRAW_BUDGET times the frame area, within [MIN_PIXEL_BUDGET, MAX_PIXEL_BUDGET]. */
#define OVERDRAW_BUDGET 10.0f
#define MIN_PIXEL_BUDGET 250000.0f
#define MAX_PIXEL_BUDGET 3000000.0f

struct Recorder;

typedef struct {
    int w, h;
    float *acc;
    Vec3 eye, right, up, fwd;
    float focal, cx, cy, near_z;
    float time;
    int thinned;
    float budget; /* sprite pixels left for this frame */
    /* Rows [ylo, yhi) that primitives may write. Everything else (projection, culling,
       thinning) always uses the whole frame, so a pixel gets exactly the same operations
       in the same order whichever strip draws it. */
    int ylo, yhi;
    struct Recorder *rec; /* non-NULL: primitives are recorded instead of drawn */
} View;

typedef struct {
    float x, y, iz, uz, vz, kz;
} RVert;

static int project(const View *v, Vec3 p, float *sx, float *sy, float *z) {
    Vec3 d = v3sub(p, v->eye);
    float zc = v3dot(d, v->fwd);
    if (!(zc > v->near_z)) return 0;
    *sx = v->cx + v3dot(d, v->right) / zc * v->focal;
    *sy = v->cy - v3dot(d, v->up) / zc * v->focal;
    *z = zc;
    return finitef(*sx) && finitef(*sy);
}

static inline uint32_t load_texel(const uint8_t *p) {
    uint32_t t;
    memcpy(&t, p, 4);
    return t;
}

static void sample(const Material *m, int level, float u, float v, int wrap, float out[4]) {
    int w = m->mip_w[level], h = m->mip_h[level], x0, y0, x1, y1, k;
    const uint8_t *px = m->mip_data[level], *a, *b, *c, *d;
    float x, y, fx, fy;
    if (wrap) {
        u -= floorf(u);
        v -= floorf(v);
    }
    x = u * (float)w - 0.5f;
    y = v * (float)h - 0.5f;
    x = clampf(x, -1.0f, (float)w);
    y = clampf(y, -1.0f, (float)h);
    x0 = clamp_int(floorf(x), -1, w);
    y0 = clamp_int(floorf(y), -1, h);
    fx = x - (float)x0;
    fy = y - (float)y0;
    x1 = x0 + 1;
    y1 = y0 + 1;
    if (wrap) {
        x0 = ((x0 % w) + w) % w;
        x1 = ((x1 % w) + w) % w;
        y0 = ((y0 % h) + h) % h;
        y1 = ((y1 % h) + h) % h;
    } else {
        x0 = x0 < 0 ? 0 : (x0 >= w ? w - 1 : x0);
        x1 = x1 < 0 ? 0 : (x1 >= w ? w - 1 : x1);
        y0 = y0 < 0 ? 0 : (y0 >= h ? h - 1 : y0);
        y1 = y1 < 0 ? 0 : (y1 >= h ? h - 1 : y1);
    }
    a = px + ((size_t)y0 * w + x0) * 4;
    b = px + ((size_t)y0 * w + x1) * 4;
    c = px + ((size_t)y1 * w + x0) * 4;
    d = px + ((size_t)y1 * w + x1) * 4;
    if (fx == fx && fy == fy && !(load_texel(a) | load_texel(b) | load_texel(c) | load_texel(d))) {
        /* four transparent texels and finite weights: the lerps below give exactly 0 */
        out[0] = out[1] = out[2] = out[3] = 0.0f;
        return;
    }
    for (k = 0; k < 4; k++) {
        float top = a[k] + (b[k] - a[k]) * fx, bot = c[k] + (d[k] - c[k]) * fx;
        out[k] = (top + (bot - top) * fy) * (1.0f / 255.0f);
    }
}

static int pick_level(const Material *m, float texels_per_pixel) {
    int level = 0;
    while (texels_per_pixel > 1.5f && level < m->mips - 1) {
        texels_per_pixel *= 0.5f;
        level++;
    }
    return level;
}

static inline void blend(float *dst, float r, float g, float b, float a, int additive) {
    if (additive) {
        dst[0] += r;
        dst[1] += g;
        dst[2] += b;
    } else {
        float ia = 1.0f - a;
        dst[0] = dst[0] * ia + r;
        dst[1] = dst[1] * ia + g;
        dst[2] = dst[2] * ia + b;
    }
}

typedef struct {
    const Material *m;
    int additive, level, wrap;
    const SheetFrame *fa, *fb;
    float mix;
    float rgb[3]; /* colour * alpha * overbright */
    float alpha;
} Paint;

/* ------------------------------------------------------------------ recorded draw commands */

enum { CMD_SPRITE = 1, CMD_TRI, CMD_LINE };

typedef struct {
    int kind;
    int ymin, ymax; /* rows the primitive can touch (conservative), clamped to [-1, h] */
    Paint pt;
    union {
        struct {
            float sx, sy, rad, ang;
            int flip;
        } s;
        struct {
            RVert a, b, c;
        } t;
        struct {
            float x0, y0, x1, y1, rgb[3], alpha;
        } l;
    } u;
} Cmd;

/* recording stops here and the frame is drawn single-threaded instead (about 9 MB of commands) */
#define CMD_LIMIT 65536

typedef struct Recorder {
    Scene *sc;
    Cmd *cmds;
    int n, cap, overflow;
} Recorder;

static Cmd *record(View *v, int kind, int ymin, int ymax) {
    Recorder *r = v->rec;
    Cmd *c;
    if (r->overflow) return NULL;
    if (r->n >= r->cap) {
        int cap = r->cap ? r->cap * 2 : 1024;
        Cmd *grown;
        if (cap > CMD_LIMIT) cap = CMD_LIMIT;
        if (r->n >= cap) {
            r->overflow = 1;
            return NULL;
        }
        grown = (Cmd *)realloc(r->cmds, (size_t)cap * sizeof(Cmd));
        if (!grown) {
            r->overflow = 1;
            return NULL;
        }
        r->cmds = grown;
        r->cap = cap;
        r->sc->cmds = grown;
        r->sc->cmds_cap = cap;
    }
    c = &r->cmds[r->n++];
    c->kind = kind;
    c->ymin = ymin;
    c->ymax = ymax;
    return c;
}

static void frame_rect(const SheetFrame *f, float r[4]) {
    if (f) {
        r[0] = f->u0;
        r[1] = f->v0;
        r[2] = f->u1;
        r[3] = f->v1;
    } else {
        r[0] = r[1] = 0.0f;
        r[2] = r[3] = 1.0f;
    }
}

/* sample the (possibly blended) sheet frame at local uv in [0,1] */
static void paint_sample(const Paint *pt, float u, float v, float out[4]) {
    float ra[4];
    frame_rect(pt->fa, ra);
    sample(pt->m, pt->level, ra[0] + (ra[2] - ra[0]) * u, ra[1] + (ra[3] - ra[1]) * v, pt->wrap, out);
    if (pt->fb && pt->fb != pt->fa && pt->mix > 0.001f) {
        float rb[4], o2[4];
        int k;
        frame_rect(pt->fb, rb);
        sample(pt->m, pt->level, rb[0] + (rb[2] - rb[0]) * u, rb[1] + (rb[3] - rb[1]) * v, pt->wrap, o2);
        for (k = 0; k < 4; k++) out[k] += (o2[k] - out[k]) * pt->mix;
    }
}

/* lerp of two packed RGBA8 texels, two channels per 16-bit lane; w in [0, 256] */
static inline uint32_t lerp_texel(uint32_t p, uint32_t q, uint32_t w) {
    uint32_t iw = 256u - w;
    uint32_t rb = (((p & 0x00ff00ffu) * iw + (q & 0x00ff00ffu) * w) >> 8) & 0x00ff00ffu;
    uint32_t ag = ((((p >> 8) & 0x00ff00ffu) * iw + ((q >> 8) & 0x00ff00ffu) * w) >> 8) & 0x00ff00ffu;
    return rb | (ag << 8);
}

/* clamped bilinear fetch in texel space (premultiplied RGBA8), fixed-point weights.
   Returns 0 when all four texels are fully transparent, so the caller can skip the pixel. */
static inline int fetch(const uint8_t *px, int w, int h, float x, float y, float out[4]) {
    int x0, y0, x1, y1, xi, yi;
    uint32_t a, b, c, d, t, fx, fy;
    x -= 0.5f;
    y -= 0.5f;
    /* NaN-safe clamps: comparisons with NaN are false */
    x = x > 0.0f ? (x < (float)(w - 1) ? x : (float)(w - 1)) : 0.0f;
    y = y > 0.0f ? (y < (float)(h - 1) ? y : (float)(h - 1)) : 0.0f;
    /* 24.8 fixed point: one conversion gives both the texel and the weight */
    xi = (int)(x * 256.0f);
    yi = (int)(y * 256.0f);
    x0 = xi >> 8;
    y0 = yi >> 8;
    fx = (uint32_t)(xi & 255);
    fy = (uint32_t)(yi & 255);
    x1 = x0 + 1 < w ? x0 + 1 : x0;
    y1 = y0 + 1 < h ? y0 + 1 : y0;
    a = load_texel(px + ((size_t)y0 * w + x0) * 4);
    b = load_texel(px + ((size_t)y0 * w + x1) * 4);
    c = load_texel(px + ((size_t)y1 * w + x0) * 4);
    d = load_texel(px + ((size_t)y1 * w + x1) * 4);
    if (!(a | b | c | d)) return 0;
    t = lerp_texel(lerp_texel(a, b, fx), lerp_texel(c, d, fx), fy);
    out[0] = (float)(t & 255u);
    out[1] = (float)((t >> 8) & 255u);
    out[2] = (float)((t >> 16) & 255u);
    out[3] = (float)(t >> 24);
    return 1;
}

#if defined(__GNUC__)
#define PP_ALWAYS_INLINE static inline __attribute__((always_inline))
#else
#define PP_ALWAYS_INLINE static inline
#endif

/* per-sprite constants for sprite_row */
typedef struct {
    const uint8_t *px;
    int tw, th;
    float ua, va, sua, sva, ub, vb, sub, svb, mix;
    float kr, kg, kb, ka;
} SpriteTex;

/* One row of a screen sprite. `additive` and `blend2` are compile-time constants at every
   call site, so each combination gets its own branch-free inner loop; the arithmetic is
   exactly the same as the generic loop it replaced. */
PP_ALWAYS_INLINE void sprite_row(float *row, int count, float lx, float ly, float dlx, float dly, const SpriteTex *st,
                                 int additive, int blend2) {
    int x;
    for (x = 0; x < count; x++, row += 3, lx += dlx, ly -= dly) {
        float t[4], cx = clampf(lx, -1.0f, 1.0f), cy = clampf(ly, -1.0f, 1.0f);
        int any = fetch(st->px, st->tw, st->th, st->ua + cx * st->sua, st->va + cy * st->sva, t);
        if (blend2) {
            float t2[4];
            if (!fetch(st->px, st->tw, st->th, st->ub + cx * st->sub, st->vb + cy * st->svb, t2)) {
                if (!any) continue;
                t2[0] = t2[1] = t2[2] = t2[3] = 0.0f;
            } else if (!any) {
                t[0] = t[1] = t[2] = t[3] = 0.0f;
            }
            t[0] += (t2[0] - t[0]) * st->mix;
            t[1] += (t2[1] - t[1]) * st->mix;
            t[2] += (t2[2] - t[2]) * st->mix;
            t[3] += (t2[3] - t[3]) * st->mix;
        } else if (!any) {
            continue; /* premultiplied: a transparent texel changes nothing */
        }
        if (additive) {
            row[0] += t[0] * st->kr;
            row[1] += t[1] * st->kg;
            row[2] += t[2] * st->kb;
        } else {
            float ia = 1.0f - t[3] * st->ka;
            row[0] = row[0] * ia + t[0] * st->kr;
            row[1] = row[1] * ia + t[1] * st->kg;
            row[2] = row[2] * ia + t[2] * st->kb;
        }
    }
}

/* dx range where |c*dx + s*dy| <= r and |c*dy - s*dx| <= r; returns 0 when empty */
static int sprite_span(float c, float s, float dy, float r, float *lo, float *hi) {
    float a, b, t;
    *lo = -1e30f;
    *hi = 1e30f;
    if (fabsf(c) > 1e-6f) {
        a = (-r - s * dy) / c;
        b = (r - s * dy) / c;
        if (a > b) t = a, a = b, b = t;
        if (a > *lo) *lo = a;
        if (b < *hi) *hi = b;
    } else if (fabsf(s * dy) > r) {
        return 0;
    }
    if (fabsf(s) > 1e-6f) {
        a = (c * dy - r) / s;
        b = (c * dy + r) / s;
        if (a > b) t = a, a = b, b = t;
        if (a > *lo) *lo = a;
        if (b < *hi) *hi = b;
    } else if (fabsf(c * dy) > r) {
        return 0;
    }
    return *lo <= *hi;
}

static void draw_screen_sprite(View *v, const Paint *pt, float sx, float sy, float rad, float ang, int flip) {
    const Material *m = pt->m;
    const uint8_t *pxa = m->mip_data[pt->level];
    int tw = m->mip_w[pt->level], th = m->mip_h[pt->level], y0, y1, y, blend2;
    float c, s, inv, ext, scale = 1.0f, ra[4], rb[4], ua, ub, va, vb, sua, sub, sva, svb, kr, kg, kb, ka, mix, dlx, dly;
    SpriteTex st;
    if (v->rec) {
        /* the drawn extent is at most rad * sqrt(2) + 1 around the centre (rad >= 0.5) */
        float e = fmaxf(rad, 0.5f) * 1.5f + 2.0f;
        Cmd *cmd = record(v, CMD_SPRITE, clamp_int(floorf(sy - e), -1, v->h), clamp_int(ceilf(sy + e), -1, v->h));
        if (cmd) {
            cmd->pt = *pt;
            cmd->u.s.sx = sx;
            cmd->u.s.sy = sy;
            cmd->u.s.rad = rad;
            cmd->u.s.ang = ang;
            cmd->u.s.flip = flip;
        }
        return;
    }
    if (rad < 0.5f) {
        /* keep sub-pixel sparks visible by trading size for opacity */
        scale = (rad * rad) / 0.25f;
        rad = 0.5f;
    }
    pp_sincos(ang, &s, &c);
    ext = rad * (fabsf(c) + fabsf(s)) + 1.0f;
    y0 = clamp_int(floorf(sy - ext), -1, v->h);
    y1 = clamp_int(ceilf(sy + ext), -1, v->h);
    if (y1 < 0 || y0 >= v->h || sx + ext < 0 || sx - ext > (float)v->w) return;
    if (y0 < v->ylo) y0 = v->ylo;
    if (y1 > v->yhi - 1) y1 = v->yhi - 1;
    if (y0 > y1) return;
    inv = 1.0f / rad;
    frame_rect(pt->fa, ra);
    frame_rect(pt->fb ? pt->fb : pt->fa, rb);
    mix = pt->mix;
    blend2 = pt->fb && pt->fb != pt->fa && mix > 0.001f;
    /* texel coordinate = centre + local * half extent (local in [-1,1]) */
    ua = (ra[0] + ra[2]) * 0.5f * tw, sua = (ra[2] - ra[0]) * 0.5f * tw * (flip ? -1.0f : 1.0f);
    va = (ra[1] + ra[3]) * 0.5f * th, sva = (ra[3] - ra[1]) * 0.5f * th;
    ub = (rb[0] + rb[2]) * 0.5f * tw, sub = (rb[2] - rb[0]) * 0.5f * tw * (flip ? -1.0f : 1.0f);
    vb = (rb[1] + rb[3]) * 0.5f * th, svb = (rb[3] - rb[1]) * 0.5f * th;
    kr = pt->rgb[0] * scale * (1.0f / 255.0f);
    kg = pt->rgb[1] * scale * (1.0f / 255.0f);
    kb = pt->rgb[2] * scale * (1.0f / 255.0f);
    ka = pt->alpha * scale * (1.0f / 255.0f);
    st.px = pxa;
    st.tw = tw;
    st.th = th;
    st.ua = ua, st.va = va, st.sua = sua, st.sva = sva;
    st.ub = ub, st.vb = vb, st.sub = sub, st.svb = svb;
    st.mix = mix;
    st.kr = kr, st.kg = kg, st.kb = kb, st.ka = ka;
    dlx = c * inv;
    dly = s * inv;
    for (y = y0; y <= y1; y++) {
        float dy = (float)y + 0.5f - sy, lo, hi, lx, ly, dxs;
        int x0, x1;
        float *row;
        if (!sprite_span(c, s, dy, rad, &lo, &hi)) continue;
        x0 = clamp_int(ceilf(sx + lo - 0.5f), -1, v->w);
        x1 = clamp_int(floorf(sx + hi - 0.5f), -1, v->w);
        if (x0 < 0) x0 = 0;
        if (x1 > v->w - 1) x1 = v->w - 1;
        if (x0 > x1) continue;
        row = v->acc + ((size_t)y * v->w + x0) * 3;
        dxs = (float)x0 + 0.5f - sx;
        lx = (c * dxs + s * dy) * inv;
        ly = (-s * dxs + c * dy) * inv;
        if (pt->additive) {
            if (blend2)
                sprite_row(row, x1 - x0 + 1, lx, ly, dlx, dly, &st, 1, 1);
            else
                sprite_row(row, x1 - x0 + 1, lx, ly, dlx, dly, &st, 1, 0);
        } else {
            if (blend2)
                sprite_row(row, x1 - x0 + 1, lx, ly, dlx, dly, &st, 0, 1);
            else
                sprite_row(row, x1 - x0 + 1, lx, ly, dlx, dly, &st, 0, 0);
        }
    }
}

static float edge(const RVert *a, const RVert *b, float px, float py) {
    return (b->x - a->x) * (py - a->y) - (b->y - a->y) * (px - a->x);
}

/* Conservative per-row x range for draw_tri.
   The inside test stays exactly as it was; this only skips pixels that provably fail it,
   so long thin ribbons (trails, ropes) no longer scan their whole bounding box.
   Each barycentric weight is linear along a row. It is evaluated in double together with
   a bound on the rounding error of the float code, and a pixel is skipped only when the
   weight is below -1e-5 by more than that bound. */
typedef struct {
    int ok;
    double k[3], m0[3], my[3], err[3];
} TriSpan;

static void tri_span_setup(TriSpan *ts, const RVert *a, const RVert *b, const RVert *c, float area, float inv, int x0,
                           int x1, int y0, int y1) {
    const RVert *from[3] = {b, c, a}, *to[3] = {c, a, b};
    const double u = 5.9604644775390625e-08; /* 2^-24, float unit roundoff */
    double pxs[2], pys[2], wmax = 0.0, dinv = (double)inv, A = (double)area;
    int e, i, j;
    ts->ok = 0;
    if (!finitef(a->x) || !finitef(a->y) || !finitef(b->x) || !finitef(b->y) || !finitef(c->x) || !finitef(c->y))
        return;
    if (!finitef(inv) || inv == 0.0f || !finitef(area)) return;
    if (fabsf(a->x) > 1e7f || fabsf(a->y) > 1e7f || fabsf(b->x) > 1e7f || fabsf(b->y) > 1e7f || fabsf(c->x) > 1e7f ||
        fabsf(c->y) > 1e7f)
        return;
    pxs[0] = (double)x0 + 0.5, pxs[1] = (double)x1 + 0.5;
    pys[0] = (double)y0 + 0.5, pys[1] = (double)y1 + 0.5;
    /* weights for edges b->c (wa) and c->a (wb): w = ((tx-fx)*(py-fy) - (ty-fy)*(px-fx)) / area */
    for (e = 0; e < 2; e++) {
        double ex = (double)to[e]->x - (double)from[e]->x, ey = (double)to[e]->y - (double)from[e]->y;
        double fx = from[e]->x, fy = from[e]->y, t1 = 0.0, t2 = 0.0;
        ts->k[e] = -ey / A;
        ts->my[e] = ex / A;
        ts->m0[e] = (-ex * fy + ey * fx) / A;
        for (i = 0; i < 2; i++) {
            double d1 = fabs(pys[i] - fy), d2 = fabs(pxs[i] - fx);
            if (d1 > t1) t1 = d1;
            if (d2 > t2) t2 = d2;
        }
        /* float edge(): rounded differences, products and subtraction */
        ts->err[e] = 8.0 * u * (fabs(ex) * t1 + fabs(ey) * t2) * fabs(dinv);
        for (i = 0; i < 2; i++)
            for (j = 0; j < 2; j++) {
                double w = fabs(ts->k[e] * pxs[i] + ts->my[e] * pys[j] + ts->m0[e]);
                if (w > wmax) wmax = w;
            }
    }
    /* multiplication by the rounded 1/area */
    ts->err[0] += 4.0 * u * wmax;
    ts->err[1] += 4.0 * u * wmax;
    /* wc = 1 - wa - wb */
    ts->k[2] = -(ts->k[0] + ts->k[1]);
    ts->my[2] = -(ts->my[0] + ts->my[1]);
    ts->m0[2] = 1.0 - ts->m0[0] - ts->m0[1];
    ts->err[2] = ts->err[0] + ts->err[1] + 4.0 * u * (1.0 + 2.0 * wmax);
    for (e = 0; e < 3; e++) {
        ts->err[e] = ts->err[e] * 4.0 + 1e-9; /* generous safety factor */
        if (!(ts->err[e] < 1e30) || !(fabs(ts->k[e]) < 1e30) || !(fabs(ts->my[e]) < 1e30) || !(fabs(ts->m0[e]) < 1e30))
            return;
    }
    ts->ok = 1;
}

/* narrows [*lo, *hi] for row y; returns 0 when no pixel of the row can pass */
static int tri_span_row(const TriSpan *ts, int y, int *lo, int *hi) {
    double py = (double)y + 0.5, xl = (double)*lo, xh = (double)*hi;
    int e;
    for (e = 0; e < 3; e++) {
        /* need k*px + m >= -1e-5 - err, px = x + 0.5 */
        double m = ts->my[e] * py + ts->m0[e], thr = -1e-5 - ts->err[e] - m, k = ts->k[e];
        if (k > 0.0) {
            double x = floor(thr / k - 0.5) - 1.0;
            if (x > xl) xl = x;
        } else if (k < 0.0) {
            double x = ceil(thr / k - 0.5) + 1.0;
            if (x < xh) xh = x;
        } else if (thr > 0.0) {
            return 0;
        }
        if (!(xl <= xh)) return 0;
    }
    *lo = (int)xl;
    *hi = (int)xh;
    return 1;
}

static void draw_tri(View *v, const Paint *pt, const RVert *a, const RVert *b, const RVert *c) {
    float area, inv;
    int x0, x1, y0, y1, x, y, skip_clear;
    TriSpan ts;
    if (v->rec) {
        Cmd *cmd = record(v, CMD_TRI, clamp_int(floorf(fminf(a->y, fminf(b->y, c->y))), -1, v->h),
                          clamp_int(ceilf(fmaxf(a->y, fmaxf(b->y, c->y))), -1, v->h));
        if (cmd) {
            cmd->pt = *pt;
            cmd->u.t.a = *a;
            cmd->u.t.b = *b;
            cmd->u.t.c = *c;
        }
        return;
    }
    area = edge(a, b, c->x, c->y);
    if (!(area > 1e-6f || area < -1e-6f)) return;
    inv = 1.0f / area;
    x0 = clamp_int(floorf(fminf(a->x, fminf(b->x, c->x))), -1, v->w);
    x1 = clamp_int(ceilf(fmaxf(a->x, fmaxf(b->x, c->x))), -1, v->w);
    y0 = clamp_int(floorf(fminf(a->y, fminf(b->y, c->y))), -1, v->h);
    y1 = clamp_int(ceilf(fmaxf(a->y, fmaxf(b->y, c->y))), -1, v->h);
    if (x1 < 0 || y1 < 0 || x0 >= v->w || y0 >= v->h) return;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > v->w - 1) x1 = v->w - 1;
    if (y1 > v->h - 1) y1 = v->h - 1;
    /* the span only pays off when rows are long; tiny triangles keep the plain scan */
    ts.ok = 0;
    if (x1 - x0 >= 16) tri_span_setup(&ts, a, b, c, area, inv, x0, x1, y0, y1);
    /* clip to the strip only after the span setup, so it sees the same rows as a whole frame */
    if (y0 < v->ylo) y0 = v->ylo;
    if (y1 > v->yhi - 1) y1 = v->yhi - 1;
    /* a fully transparent sample adds exactly zero when the paint factors are finite */
    skip_clear = finitef(pt->rgb[0]) && finitef(pt->rgb[1]) && finitef(pt->rgb[2]) && finitef(pt->alpha);
    for (y = y0; y <= y1; y++) {
        float py = (float)y + 0.5f;
        float *row = v->acc + ((size_t)y * v->w) * 3;
        int xs = x0, xe = x1;
        if (ts.ok && !tri_span_row(&ts, y, &xs, &xe)) continue;
        for (x = xs; x <= xe; x++) {
            float px = (float)x + 0.5f, wa = edge(b, c, px, py) * inv, wb = edge(c, a, px, py) * inv, wc = 1.0f - wa - wb;
            float iz, u, vv, k, t[4];
            if (wa < -1e-5f || wb < -1e-5f || wc < -1e-5f) continue;
            iz = wa * a->iz + wb * b->iz + wc * c->iz;
            if (!(iz > 0.0f)) continue;
            u = (wa * a->uz + wb * b->uz + wc * c->uz) / iz;
            vv = (wa * a->vz + wb * b->vz + wc * c->vz) / iz;
            k = (wa * a->kz + wb * b->kz + wc * c->kz) / iz;
            paint_sample(pt, u, vv, t);
            if (skip_clear && t[0] == 0.0f && t[1] == 0.0f && t[2] == 0.0f && t[3] == 0.0f && finitef(k)) continue;
            blend(row + (size_t)x * 3, t[0] * pt->rgb[0] * k, t[1] * pt->rgb[1] * k, t[2] * pt->rgb[2] * k,
                  t[3] * pt->alpha * k, pt->additive);
        }
    }
}

static RVert rvert(float x, float y, float z, float u, float vv, float k) {
    RVert r;
    r.x = x;
    r.y = y;
    r.iz = 1.0f / z;
    r.uz = u * r.iz;
    r.vz = vv * r.iz;
    r.kz = k * r.iz;
    return r;
}

/* world-space quad; corners in order around the quad */
static void draw_world_quad(View *v, Paint *pt, const Vec3 q[4], const float uv[4][2], float texels) {
    float sx[4], sy[4], sz[4], len;
    RVert r[4];
    int k;
    for (k = 0; k < 4; k++) {
        if (!project(v, q[k], &sx[k], &sy[k], &sz[k])) return;
        if (fabsf(sx[k]) > 1e6f || fabsf(sy[k]) > 1e6f) return;
    }
    len = sqrtf((sx[1] - sx[0]) * (sx[1] - sx[0]) + (sy[1] - sy[0]) * (sy[1] - sy[0]));
    len = fmaxf(len, sqrtf((sx[2] - sx[1]) * (sx[2] - sx[1]) + (sy[2] - sy[1]) * (sy[2] - sy[1])));
    pt->level = pick_level(pt->m, len > 0.5f ? texels / len : texels);
    for (k = 0; k < 4; k++) r[k] = rvert(sx[k], sy[k], sz[k], uv[k][0], uv[k][1], 1.0f);
    draw_tri(v, pt, &r[0], &r[1], &r[2]);
    draw_tri(v, pt, &r[0], &r[2], &r[3]);
}

/* screen-space ribbon between two points with widths in world units */
static void draw_ribbon(View *v, Paint *pt, Vec3 a, Vec3 b, float wa, float wb, float va, float vb, float ka, float kb) {
    float ax, ay, az, bx, by, bz, dx, dy, l, nx, ny, ra, rb;
    RVert r[4];
    if (!project(v, a, &ax, &ay, &az) || !project(v, b, &bx, &by, &bz)) return;
    dx = bx - ax;
    dy = by - ay;
    l = sqrtf(dx * dx + dy * dy);
    if (l < 1e-3f) return;
    nx = -dy / l;
    ny = dx / l;
    ra = wa * v->focal / az;
    rb = wb * v->focal / bz;
    if (!(ra < 4.0f * (float)(v->w + v->h)) || !(rb < 4.0f * (float)(v->w + v->h))) return;
    if (ra < 0.5f) ra = 0.5f;
    if (rb < 0.5f) rb = 0.5f;
    r[0] = rvert(ax + nx * ra, ay + ny * ra, az, 0.0f, va, ka);
    r[1] = rvert(ax - nx * ra, ay - ny * ra, az, 1.0f, va, ka);
    r[2] = rvert(bx - nx * rb, by - ny * rb, bz, 1.0f, vb, kb);
    r[3] = rvert(bx + nx * rb, by + ny * rb, bz, 0.0f, vb, kb);
    draw_tri(v, pt, &r[0], &r[1], &r[2]);
    draw_tri(v, pt, &r[0], &r[2], &r[3]);
}

/* ------------------------------------------------------------------ background */

static void draw_line2d(View *v, float x0, float y0, float x1, float y1, const float rgb[3], float alpha) {
    float dx = x1 - x0, dy = y1 - y0, steps = fmaxf(fabsf(dx), fabsf(dy)), t;
    int n, i;
    if (v->rec) {
        Cmd *cmd = record(v, CMD_LINE, 0, v->h - 1); /* a few dozen per frame: no row bounds */
        if (cmd) {
            memset(&cmd->pt, 0, sizeof(cmd->pt));
            cmd->u.l.x0 = x0;
            cmd->u.l.y0 = y0;
            cmd->u.l.x1 = x1;
            cmd->u.l.y1 = y1;
            cmd->u.l.rgb[0] = rgb[0];
            cmd->u.l.rgb[1] = rgb[1];
            cmd->u.l.rgb[2] = rgb[2];
            cmd->u.l.alpha = alpha;
        }
        return;
    }
    if (!finitef(steps)) return;
    if (steps > 4.0f * (v->w + v->h)) {
        /* clip very long lines to the screen rectangle in parameter space */
        float lo = 0.0f, hi = 1.0f, p[4], q[4];
        int k;
        p[0] = -dx, q[0] = x0;
        p[1] = dx, q[1] = (float)v->w - 1 - x0;
        p[2] = -dy, q[2] = y0;
        p[3] = dy, q[3] = (float)v->h - 1 - y0;
        for (k = 0; k < 4; k++) {
            if (fabsf(p[k]) < 1e-9f) {
                if (q[k] < 0) return;
                continue;
            }
            t = q[k] / p[k];
            if (p[k] < 0) {
                if (t > lo) lo = t;
            } else if (t < hi) {
                hi = t;
            }
        }
        if (lo > hi) return;
        x1 = x0 + dx * hi;
        y1 = y0 + dy * hi;
        x0 = x0 + dx * lo;
        y0 = y0 + dy * lo;
        dx = x1 - x0;
        dy = y1 - y0;
        steps = fmaxf(fabsf(dx), fabsf(dy));
    }
    n = (int)steps + 1;
    for (i = 0; i <= n; i++) {
        float f = n ? (float)i / (float)n : 0.0f;
        int x = (int)(x0 + dx * f), y = (int)(y0 + dy * f);
        float *p;
        if (x < 0 || y < 0 || x >= v->w || y >= v->h || y < v->ylo || y >= v->yhi) continue;
        p = v->acc + ((size_t)y * v->w + x) * 3;
        p[0] += (rgb[0] - p[0]) * alpha;
        p[1] += (rgb[1] - p[1]) * alpha;
        p[2] += (rgb[2] - p[2]) * alpha;
    }
}

static void draw_line3d(View *v, Vec3 a, Vec3 b, const float rgb[3], float alpha) {
    float da = v3dot(v3sub(a, v->eye), v->fwd) - v->near_z * 2.0f, db = v3dot(v3sub(b, v->eye), v->fwd) - v->near_z * 2.0f;
    float ax, ay, az, bx, by, bz;
    if (da < 0 && db < 0) return;
    if (da < 0) a = v3lerp(a, b, da / (da - db));
    if (db < 0) b = v3lerp(b, a, db / (db - da));
    if (!project(v, a, &ax, &ay, &az) || !project(v, b, &bx, &by, &bz)) return;
    draw_line2d(v, ax, ay, bx, by, rgb, alpha);
}

/* a two pixel wide line: the second pass is offset across the line's main direction */
static void draw_line3d_wide(View *v, Vec3 a, Vec3 b, const float rgb[3], float alpha) {
    float da = v3dot(v3sub(a, v->eye), v->fwd) - v->near_z * 2.0f, db = v3dot(v3sub(b, v->eye), v->fwd) - v->near_z * 2.0f;
    float ax, ay, az, bx, by, bz;
    if (da < 0 && db < 0) return;
    if (da < 0) a = v3lerp(a, b, da / (da - db));
    if (db < 0) b = v3lerp(b, a, db / (db - da));
    if (!project(v, a, &ax, &ay, &az) || !project(v, b, &bx, &by, &bz)) return;
    draw_line2d(v, ax, ay, bx, by, rgb, alpha);
    if (fabsf(bx - ax) > fabsf(by - ay))
        draw_line2d(v, ax, ay + 1.0f, bx, by + 1.0f, rgb, alpha);
    else
        draw_line2d(v, ax + 1.0f, ay, bx + 1.0f, by, rgb, alpha);
}

/* vertical gradient over the rows [v->ylo, v->yhi) */
static void draw_gradient(const Scene *sc, View *v) {
    float top[3], bot[3];
    int x, y, k;
    for (k = 0; k < 3; k++) {
        top[k] = (float)((sc->bg_top >> (16 - 8 * k)) & 255) / 255.0f;
        bot[k] = (float)((sc->bg_bottom >> (16 - 8 * k)) & 255) / 255.0f;
    }
    for (y = v->ylo; y < v->yhi; y++) {
        float t = v->h > 1 ? (float)y / (float)(v->h - 1) : 0.0f, c[3];
        float *row = v->acc + (size_t)y * v->w * 3;
        for (k = 0; k < 3; k++) c[k] = lerpf(top[k], bot[k], t);
        for (x = 0; x < v->w; x++) {
            row[x * 3] = c[0];
            row[x * 3 + 1] = c[1];
            row[x * 3 + 2] = c[2];
        }
    }
}

/* grid and axes over the gradient */
static void draw_guides(Scene *sc, View *v) {
    int k;
    if ((sc->options & PP_OPT_GRID) && (sc->options & PP_OPT_EDITOR_GRID)) {
        /* the Particle Editor's grid: every line the same bright colour, two pixels wide */
        float sp = powf(2.0f, roundf(log2f(fmaxf(sc->distance, 1.0f) / 5.0f))), ext;
        static const float line[3] = {0.97f, 0.94f, 0.94f};
        int n = 8;
        sp = clampf(sp, 1.0f, 4096.0f);
        ext = sp * (float)n;
        for (k = -n; k <= n; k++) {
            float o = sp * (float)k;
            draw_line3d_wide(v, v3(o, -ext, 0), v3(o, ext, 0), line, 0.9f);
            draw_line3d_wide(v, v3(-ext, o, 0), v3(ext, o, 0), line, 0.9f);
        }
    } else if (sc->options & PP_OPT_GRID) {
        float sp = powf(2.0f, roundf(log2f(fmaxf(sc->distance, 1.0f) / 12.0f))), ext;
        static const float minor[3] = {0.55f, 0.57f, 0.62f}, xr[3] = {0.75f, 0.3f, 0.3f}, yg[3] = {0.3f, 0.7f, 0.35f};
        int n = 16;
        sp = clampf(sp, 1.0f, 4096.0f);
        ext = sp * (float)n;
        for (k = -n; k <= n; k++) {
            float a = (k % 4 == 0) ? 0.2f : 0.09f, o = sp * (float)k;
            draw_line3d(v, v3(o, -ext, 0), v3(o, ext, 0), k == 0 ? yg : minor, k == 0 ? 0.45f : a);
            draw_line3d(v, v3(-ext, o, 0), v3(ext, o, 0), k == 0 ? xr : minor, k == 0 ? 0.45f : a);
        }
    }
    if (sc->options & PP_OPT_AXES) {
        float l = clampf(sc->distance * 0.08f, 2.0f, 256.0f);
        static const float red[3] = {1.0f, 0.25f, 0.25f}, green[3] = {0.3f, 1.0f, 0.3f}, blue[3] = {0.35f, 0.5f, 1.0f};
        Vec3 o = sc->cp[0];
        draw_line3d(v, o, v3add(o, v3(l, 0, 0)), red, 0.9f);
        draw_line3d(v, o, v3add(o, v3(0, l, 0)), green, 0.9f);
        draw_line3d(v, o, v3add(o, v3(0, 0, l)), blue, 0.9f);
    }
}

/* ------------------------------------------------------------------ particles */

typedef struct {
    float depth;
    int index;
    float sx, sy; /* projected centre, reused when drawing */
} Item;

/* shell sort, far first: avoids the CRT qsort callback */
static void sort_far_first(Item *items, int n) {
    static const int gaps[] = {1750, 701, 301, 132, 57, 23, 10, 4, 1};
    size_t g;
    for (g = 0; g < sizeof(gaps) / sizeof(gaps[0]); g++) {
        int gap = gaps[g], i;
        for (i = gap; i < n; i++) {
            Item t = items[i];
            int j = i;
            while (j >= gap && items[j - gap].depth < t.depth) {
                items[j] = items[j - gap];
                j -= gap;
            }
            items[j] = t;
        }
    }
}

static float sheet_phase(const Func *fn, const Material *m, const System *s, int i, float rate) {
    float age = s->time - s->spawn[i];
    if (fn->i[1]) return age / s->life[i];
    if (fn->i[0]) {
        int frames = pp_sheet_frame_count(m, seq_index(s->seq[i]));
        return frames > 0 ? age * rate / (float)frames : 0.0f;
    }
    return age * rate;
}

/* weight > 1 when particles were thinned out: each drawn one stands in for `weight` of them */
static void setup_paint(Paint *pt, const Material *m, const System *s, int i, float weight) {
    float a = saturatef(s->alpha[i]), ob = m->overbright > 0 ? m->overbright : 1.0f;
    if (weight > 1.0f) {
        if (m->flags & PP_MAT_ADDITIVE)
            a *= weight; /* same total light */
        else
            a = 1.0f - powf(1.0f - a, weight); /* same coverage as `weight` stacked layers */
    }
    pt->m = m;
    pt->additive = (m->flags & PP_MAT_ADDITIVE) != 0;
    pt->alpha = a;
    pt->rgb[0] = fmaxf(s->color[3 * i], 0.0f) * a * ob;
    pt->rgb[1] = fmaxf(s->color[3 * i + 1], 0.0f) * a * ob;
    pt->rgb[2] = fmaxf(s->color[3 * i + 2], 0.0f) * a * ob;
    pt->wrap = 0;
    pt->fa = pt->fb = NULL;
    pt->mix = 0.0f;
}

static void set_frames(Paint *pt, const Func *fn, const System *s, int i) {
    const Material *m = pt->m;
    if (m->seqs) {
        pt->mix = pp_sheet_frames(m, seq_index(s->seq[i]), sheet_phase(fn, m, s, i, fn->f[0]), &pt->fa, &pt->fb);
        if (m->flags & PP_MAT_NO_BLEND_FRAMES) pt->fb = pt->fa;
    }
}

static float frame_texels(const Paint *pt) {
    float r[4];
    frame_rect(pt->fa, r);
    return fabsf(r[2] - r[0]) * (float)pt->m->width;
}

static void render_sprites(View *v, const Func *fn, const Material *m, const System *s, Item *items) {
    int n = 0, k, stride = 1, orient = fn->i[2];
    float area = 0.0f, weight = 1.0f;
    for (k = 0; k < s->count; k++) {
        float sx, sy, z, rad;
        Vec3 p = v3(s->pos[3 * k], s->pos[3 * k + 1], s->pos[3 * k + 2]);
        if (s->alpha[k] <= 0.001f || s->radius[k] <= 0.0f) continue;
        if (!project(v, p, &sx, &sy, &z)) continue;
        rad = s->radius[k] * v->focal / z;
        if (!(rad < 8.0f * (float)(v->w + v->h))) continue; /* NaN, or covers the view many times over */
        if (sx + rad * 1.5f < 0 || sy + rad * 1.5f < 0 || sx - rad * 1.5f > v->w || sy - rad * 1.5f > v->h) continue;
        {
            /* pixels actually drawn: the sprite's square clipped to the view */
            float r2 = fmaxf(rad, 0.5f);
            float cw = fminf(sx + r2, (float)v->w) - fmaxf(sx - r2, 0.0f);
            float ch = fminf(sy + r2, (float)v->h) - fmaxf(sy - r2, 0.0f);
            if (cw > 0.0f && ch > 0.0f) area += cw * ch;
        }
        items[n].depth = z;
        items[n].index = k;
        items[n].sx = sx;
        items[n].sy = sy;
        n++;
    }
    if (n == 0) return;
    {
        /* later systems (children) always get a share, even after earlier ones used the budget */
        float allow = fmaxf(v->budget, MIN_PIXEL_BUDGET * 0.25f);
        if (area > allow) {
            int drawn;
            stride = clamp_int(ceilf(area / allow), 1, n);
            drawn = (n + stride - 1) / stride;
            weight = (float)n / (float)drawn;
            if (stride > 1) v->thinned = 1;
        }
        v->budget -= area / weight;
    }
    if (s->def->sort && !(m->flags & PP_MAT_ADDITIVE)) sort_far_first(items, n);
    for (k = 0; k < n; k += stride) {
        int i = items[k].index;
        Paint pt;
        Vec3 p = v3(s->pos[3 * i], s->pos[3 * i + 1], s->pos[3 * i + 2]);
        float rad = s->radius[i], ang = s->rot[i];
        int flip = fabsf(fmodf(s->yaw[i], 2.0f * PP_PI)) > PP_PI * 0.5f && fabsf(fmodf(s->yaw[i], 2.0f * PP_PI)) < PP_PI * 1.5f;
        setup_paint(&pt, m, s, i, weight);
        if (pt.alpha * fmaxf(pt.rgb[0], fmaxf(pt.rgb[1], pt.rgb[2])) < 1e-4f && pt.additive) continue;
        set_frames(&pt, fn, s, i);
        if (fn->kind == K_REN_VELOCITY_ROTATE) {
            /* p was projected when the item was collected: same inputs, same result */
            float ax = items[k].sx, ay = items[k].sy, bx, by, bz;
            Vec3 q = v3add(p, v3scale(v3(s->vel[3 * i], s->vel[3 * i + 1], s->vel[3 * i + 2]), 0.05f));
            if (project(v, q, &bx, &by, &bz) && (fabsf(bx - ax) + fabsf(by - ay)) > 1e-4f)
                ang = atan2f(by - ay, bx - ax) + fn->f[2];
            orient = 0;
        }
        if (orient == 1 || orient == 2) {
            Vec3 ax, ay, q[4];
            static const float uv[4][2] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
            float c, sn;
            pp_sincos(ang, &sn, &c);
            if (orient == 2) {
                ax = v3(c * rad, sn * rad, 0.0f);
                ay = v3(-sn * rad, c * rad, 0.0f);
            } else {
                Vec3 rt = v3norm(v3cross(v->fwd, v3(0, 0, 1)), v->right), dn = v3(0, 0, -1);
                ax = v3scale(v3add(v3scale(rt, c), v3scale(dn, -sn)), rad);
                ay = v3scale(v3add(v3scale(rt, sn), v3scale(dn, c)), rad);
            }
            q[0] = v3sub(v3sub(p, ax), ay);
            q[1] = v3sub(v3add(p, ax), ay);
            q[2] = v3add(v3add(p, ax), ay);
            q[3] = v3add(v3sub(p, ax), ay);
            draw_world_quad(v, &pt, q, uv, frame_texels(&pt));
        } else {
            float sx = items[k].sx, sy = items[k].sy, z = items[k].depth, srad;
            srad = rad * v->focal / z;
            pt.level = pick_level(m, frame_texels(&pt) / fmaxf(2.0f * srad, 1.0f));
            draw_screen_sprite(v, &pt, sx, sy, srad, ang, flip);
        }
    }
}

static void render_trails(View *v, const Func *fn, const Material *m, const System *s) {
    int i;
    for (i = 0; i < s->count; i++) {
        Vec3 p = v3(s->pos[3 * i], s->pos[3 * i + 1], s->pos[3 * i + 2]);
        Vec3 vel = v3(s->vel[3 * i], s->vel[3 * i + 1], s->vel[3 * i + 2]);
        float speed = v3len(vel), len, age = s->time - s->spawn[i], rad = s->radius[i];
        Paint pt;
        if (speed < 1e-4f || s->alpha[i] <= 0.001f) continue;
        len = clampf(s->trail[i] * speed, fn->f[2], fn->f[3]);
        if (fn->f[1] > 0.0f) len *= saturatef(age / fn->f[1]);
        if (len <= 0.0f) continue;
        if (fn->i[0] && rad > len * 0.5f) rad = len * 0.5f;
        setup_paint(&pt, m, s, i, 1.0f);
        set_frames(&pt, fn, s, i);
        pt.level = pick_level(m, frame_texels(&pt) / fmaxf(2.0f * rad * v->focal / fmaxf(v3dot(v3sub(p, v->eye), v->fwd), 1.0f), 1.0f));
        draw_ribbon(v, &pt, p, v3sub(p, v3scale(vel, len / speed)), rad, rad, 0.0f, 1.0f, 1.0f, fn->f[4]);
    }
}

static void render_rope(View *v, const Func *fn, const Material *m, const System *s) {
    int i;
    float vpos = -fn->f[1] * s->time / fn->f[0];
    for (i = 0; i + 1 < s->count; i++) {
        Vec3 a = v3(s->pos[3 * i], s->pos[3 * i + 1], s->pos[3 * i + 2]);
        Vec3 b = v3(s->pos[3 * i + 3], s->pos[3 * i + 4], s->pos[3 * i + 5]);
        float seg = v3len(v3sub(b, a)) / fn->f[0], alpha = 0.5f * (s->alpha[i] + s->alpha[i + 1]);
        Paint pt;
        if (alpha <= 0.001f) {
            vpos += seg;
            continue;
        }
        setup_paint(&pt, m, s, i, 1.0f);
        pt.wrap = 1;
        pt.level = 0;
        draw_ribbon(v, &pt, a, b, s->radius[i], s->radius[i + 1], vpos, vpos + seg, 1.0f,
                    s->alpha[i] > 0 ? s->alpha[i + 1] / s->alpha[i] : 1.0f);
        vpos += seg;
    }
}

static void render_system(Scene *sc, View *v, const System *s, Item *items) {
    const Def *d = s->def;
    const Material *m = pp_scene_material(sc, d->material);
    int f, c;
    if (!m) m = pp_default_material();
    if (m && s->count > 0 && !(m->flags & PP_MAT_INVISIBLE)) {
        for (f = 0; f < d->nfuncs; f++) {
            const Func *fn = &d->funcs[f];
            if (fn->category != CAT_RENDERER) continue;
            if (fn->kind == K_REN_SPRITES || fn->kind == K_REN_VELOCITY_ROTATE)
                render_sprites(v, fn, m, s, items);
            else if (fn->kind == K_REN_TRAIL)
                render_trails(v, fn, m, s);
            else if (fn->kind == K_REN_ROPE)
                render_rope(v, fn, m, s);
        }
    }
    for (c = 0; c < s->nchildren; c++) render_system(sc, v, s->children[c], items);
}

/* ------------------------------------------------------------------ threads */

int pp_force_threads = 0;
static int g_thread_setting = 0; /* 0 = automatic */
static int g_threads = 0;        /* resolved count, 0 = not yet */

static int logical_cores(void) {
#ifdef _WIN32
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    return (int)si.dwNumberOfProcessors;
#else
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int)n : 1;
#endif
}

#define PP_MAX_THREADS 16

int pp_render_set_threads(int count) {
    if (count < 0) count = 0;
    g_thread_setting = count;
    if (count == 0) {
        /* leave one core to SFM itself */
        count = logical_cores() - 1;
        if (count > 8) count = 8;
    }
    if (count < 1) count = 1;
    if (count > PP_MAX_THREADS) count = PP_MAX_THREADS;
    g_threads = count;
    return g_threads;
}

int pp_render_threads(void) {
    if (!g_threads) pp_render_set_threads(g_thread_setting);
    return g_threads;
}

static void convert_rows(const float *acc, unsigned char *out, int width, int stride, int y0, int y1) {
    int x, y;
    for (y = y0; y < y1; y++) {
        const float *src = acc + (size_t)y * width * 3;
        unsigned char *dst = out + (size_t)y * stride;
        for (x = 0; x < width; x++) {
            float r = saturatef(src[x * 3]), g = saturatef(src[x * 3 + 1]), b = saturatef(src[x * 3 + 2]);
            dst[x * 4] = (unsigned char)(b * 255.0f + 0.5f);
            dst[x * 4 + 1] = (unsigned char)(g * 255.0f + 0.5f);
            dst[x * 4 + 2] = (unsigned char)(r * 255.0f + 0.5f);
            dst[x * 4 + 3] = 255;
        }
    }
}

typedef struct {
    Scene *sc;
    View base;
    const Cmd *cmds;
    int ncmds;
    unsigned char *out;
    int stride, strip;
#ifdef _WIN32
    volatile LONG next;
#else
    volatile int next;
#endif
} Job;

static int take_strip(Job *job) {
#ifdef _WIN32
    return (int)InterlockedExchangeAdd(&job->next, (LONG)job->strip);
#else
    return __sync_fetch_and_add(&job->next, job->strip);
#endif
}

/* draws strips until none are left; every thread, the caller included, runs this */
static void run_job(Job *job) {
    for (;;) {
        int y0 = take_strip(job), y1, i;
        View v;
        if (y0 >= job->base.h) break;
        y1 = y0 + job->strip < job->base.h ? y0 + job->strip : job->base.h;
        v = job->base;
        v.rec = NULL;
        v.ylo = y0;
        v.yhi = y1;
        draw_gradient(job->sc, &v);
        for (i = 0; i < job->ncmds; i++) {
            const Cmd *c = &job->cmds[i];
            if (c->ymax < y0 || c->ymin >= y1) continue;
            if (c->kind == CMD_SPRITE)
                draw_screen_sprite(&v, &c->pt, c->u.s.sx, c->u.s.sy, c->u.s.rad, c->u.s.ang, c->u.s.flip);
            else if (c->kind == CMD_TRI)
                draw_tri(&v, &c->pt, &c->u.t.a, &c->u.t.b, &c->u.t.c);
            else
                draw_line2d(&v, c->u.l.x0, c->u.l.y0, c->u.l.x1, c->u.l.y1, c->u.l.rgb, c->u.l.alpha);
        }
        convert_rows(v.acc, job->out, v.w, job->stride, y0, y1);
    }
}

#ifdef _WIN32
static DWORD WINAPI thread_main(LPVOID arg) {
    run_job((Job *)arg);
    return 0;
}
#else
static void *thread_main(void *arg) {
    run_job((Job *)arg);
    return NULL;
}
#endif

/* runs the job on `threads` threads (the caller is one of them); a thread that cannot be
   started just leaves more strips to the others */
static void run_threads(Job *job, int threads) {
    int t, started = 0;
#ifdef _WIN32
    HANDLE handles[PP_MAX_THREADS];
    for (t = 1; t < threads; t++) {
        HANDLE h = CreateThread(NULL, 256 * 1024, thread_main, job, 0, NULL);
        if (h) handles[started++] = h;
    }
    run_job(job);
    for (t = 0; t < started; t++) {
        WaitForSingleObject(handles[t], INFINITE);
        CloseHandle(handles[t]);
    }
#else
    pthread_t ids[PP_MAX_THREADS];
    for (t = 1; t < threads; t++)
        if (pthread_create(&ids[started], NULL, thread_main, job) == 0) started++;
    run_job(job);
    for (t = 0; t < started; t++) pthread_join(ids[t], NULL);
#endif
}

static void render_particles(Scene *sc, View *v, int maxcap) {
    Item *items;
    if (sc->items_cap < maxcap) {
        /* kept between frames; pp_sim_free releases it */
        free(sc->items);
        sc->items = malloc((size_t)maxcap * sizeof(Item));
        sc->items_cap = sc->items ? maxcap : 0;
    }
    items = (Item *)sc->items;
    if (items && sc->systems && sc->nsystems > 0 && sc->systems[0].def) render_system(sc, v, &sc->systems[0], items);
}

int pp_render_scene(Scene *sc, unsigned char *out, int width, int height, int stride) {
    View v;
    float cy, sy, cp, sp;
    int need = width * height * 3, maxcap = 1, i, threads;
    pp_sincos(sc->yaw * PP_DEG, &sy, &cy);
    pp_sincos(sc->pitch * PP_DEG, &sp, &cp);
    if (!out || width < 1 || height < 1 || width > PP_MAX_RENDER_DIM || height > PP_MAX_RENDER_DIM || stride < width * 4) {
        pp_set_error("bad render target %dx%d stride %d", width, height, stride);
        return 0;
    }
    if (sc->acc_len < need) {
        float *acc = (float *)realloc(sc->acc, (size_t)need * sizeof(float));
        if (!acc) {
            pp_set_error("out of memory");
            return 0;
        }
        sc->acc = acc;
        sc->acc_len = need;
    }
    memset(&v, 0, sizeof(v));
    v.w = width;
    v.h = height;
    v.ylo = 0;
    v.yhi = height;
    v.acc = sc->acc;
    v.near_z = fmaxf(0.5f, sc->distance * 0.002f);
    v.eye = v3add(sc->target, v3scale(v3(cp * cy, cp * sy, sp), sc->distance));
    v.fwd = v3norm(v3sub(sc->target, v.eye), v3(1, 0, 0));
    v.right = v3norm(v3cross(v.fwd, v3(0, 0, 1)), v3(0, -1, 0));
    v.up = v3cross(v.right, v.fwd);
    {
        float half_s, half_c;
        pp_sincos(clampf(sc->fov, 5.0f, 150.0f) * PP_DEG * 0.5f, &half_s, &half_c);
        v.focal = (float)height * 0.5f * half_c / half_s;
    }
    v.cx = (float)width * 0.5f;
    v.cy = (float)height * 0.5f;
    v.time = sc->time;
    v.budget = clampf(OVERDRAW_BUDGET * (float)width * (float)height, MIN_PIXEL_BUDGET, MAX_PIXEL_BUDGET);
    for (i = 0; i < sc->nsystems; i++)
        if (sc->systems[i].cap > maxcap) maxcap = sc->systems[i].cap;

    threads = pp_render_threads();
    if (threads > 1 && (pp_force_threads || (width * height >= 16384 && height >= 32))) {
        /* One thread decides what to draw (culling, thinning, sorting) and records it,
           then all threads replay the commands, each into its own strips of rows. */
        Recorder rec;
        View first = v;
        rec.sc = sc;
        rec.cmds = (Cmd *)sc->cmds;
        rec.cap = sc->cmds_cap;
        rec.n = 0;
        rec.overflow = 0;
        v.rec = &rec;
        draw_guides(sc, &v);
        render_particles(sc, &v, maxcap);
        v.rec = NULL;
        if (!rec.overflow) {
            Job job;
            int strip = height / (threads * 4);
            if (strip < 8) strip = 8;
            if (pp_force_threads) strip = 3;
            job.sc = sc;
            job.base = v;
            job.cmds = rec.cmds;
            job.ncmds = rec.n;
            job.out = out;
            job.stride = stride;
            job.strip = strip;
            job.next = 0;
            run_threads(&job, threads);
            return v.thinned ? 2 : 1;
        }
        /* too many commands: start over single-threaded (same decisions, same frame) */
        v = first;
    }
    draw_gradient(sc, &v);
    draw_guides(sc, &v);
    render_particles(sc, &v, maxcap);
    convert_rows(sc->acc, out, width, stride, 0, height);
    return v.thinned ? 2 : 1;
}
