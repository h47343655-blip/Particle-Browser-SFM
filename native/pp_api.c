/* Exported API: handle tables, definition blob parsing and scene control. */
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>

#include "pp_internal.h"

#define MAX_MATERIALS 4096
#define MAX_SCENES 64
#define MAX_ISSUES 128

static char g_error[512];
static Material *g_materials[MAX_MATERIALS];
static Scene *g_scenes[MAX_SCENES];

void pp_set_error(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_error, sizeof(g_error), fmt, ap);
    va_end(ap);
}

PP_API const char *pp_last_error(void) { return g_error; }
PP_API int pp_abi_version(void) { return PP_ABI_VERSION; }

/* ------------------------------------------------------------------ arena */

struct Arena {
    struct Arena *next;
    size_t used, size;
    unsigned char data[1];
};

static void *arena_alloc(Arena **head, size_t n) {
    Arena *a = *head;
    n = (n + 7) & ~(size_t)7;
    if (!a || a->used + n > a->size) {
        size_t size = n > 65536 ? n : 65536;
        Arena *b = (Arena *)malloc(sizeof(Arena) + size);
        if (!b) return NULL;
        b->next = a;
        b->used = 0;
        b->size = size;
        *head = b;
        a = b;
    }
    a->used += n;
    return memset(a->data + a->used - n, 0, n);
}

static void arena_free(Arena *a) {
    while (a) {
        Arena *n = a->next;
        free(a);
        a = n;
    }
}

/* ------------------------------------------------------------------ materials */

Material *pp_material_lookup(int id) {
    if (id < 1 || id > MAX_MATERIALS) return NULL;
    return g_materials[id - 1];
}

static int store_material(Material *m, int flags, float overbright) {
    int i;
    m->flags = flags;
    m->overbright = overbright > 0.0f && finitef(overbright) ? overbright : 1.0f;
    for (i = 0; i < MAX_MATERIALS; i++) {
        if (!g_materials[i]) {
            Material *slot = (Material *)malloc(sizeof(Material));
            if (!slot) break;
            *slot = *m;
            g_materials[i] = slot;
            return i + 1;
        }
    }
    pp_material_release(m);
    pp_set_error("too many materials");
    return 0;
}

PP_API int pp_material_create_vtf(const unsigned char *data, int size, int flags, float overbright) {
    Material m;
    if (!pp_vtf_decode(data, size, &m)) return 0;
    return store_material(&m, flags, overbright);
}

PP_API int pp_material_create_rgba(const unsigned char *rgba, int width, int height, int flags, float overbright) {
    Material m;
    if (!pp_material_from_rgba(rgba, width, height, &m)) return 0;
    return store_material(&m, flags, overbright);
}

PP_API void pp_material_destroy(int id) {
    Material *m = pp_material_lookup(id);
    if (!m) return;
    pp_material_release(m);
    free(m);
    g_materials[id - 1] = NULL;
}

PP_API int pp_material_info(int id, int *width, int *height, int *sequences, int *mips) {
    Material *m = pp_material_lookup(id);
    int s, n = 0;
    if (!m) return 0;
    if (width) *width = m->width;
    if (height) *height = m->height;
    if (mips) *mips = m->mips;
    if (m->seqs)
        for (s = 0; s < m->nseq; s++) n += m->seqs[s].count > 0;
    if (sequences) *sequences = n;
    return 1;
}

PP_API int pp_material_live_count(void) {
    int i, n = 0;
    for (i = 0; i < MAX_MATERIALS; i++) n += g_materials[i] != NULL;
    return n;
}

/* ------------------------------------------------------------------ blob reader */

typedef struct {
    const unsigned char *p, *end;
    int ok;
} Reader;

static int need(Reader *r, size_t n) {
    if (!r->ok || (size_t)(r->end - r->p) < n) {
        r->ok = 0;
        return 0;
    }
    return 1;
}

static uint32_t rd_u32(Reader *r) {
    uint32_t v;
    if (!need(r, 4)) return 0;
    v = (uint32_t)r->p[0] | ((uint32_t)r->p[1] << 8) | ((uint32_t)r->p[2] << 16) | ((uint32_t)r->p[3] << 24);
    r->p += 4;
    return v;
}

static int rd_u8(Reader *r) {
    if (!need(r, 1)) return 0;
    return *r->p++;
}

static float rd_f32(Reader *r) {
    uint32_t u = rd_u32(r);
    float f;
    memcpy(&f, &u, 4);
    return finitef(f) ? f : 0.0f;
}

static const char *rd_str(Reader *r, Arena **arena) {
    unsigned len;
    char *s;
    if (!need(r, 2)) return NULL;
    len = (unsigned)r->p[0] | ((unsigned)r->p[1] << 8);
    r->p += 2;
    if (len > 4096 || !need(r, len)) {
        r->ok = 0;
        return NULL;
    }
    s = (char *)arena_alloc(arena, len + 1);
    if (!s) {
        r->ok = 0;
        return NULL;
    }
    memcpy(s, r->p, len);
    s[len] = 0;
    r->p += len;
    return s;
}

static int rd_attrs(Reader *r, Arena **arena, AttrList *out) {
    uint32_t n = rd_u32(r), i;
    Attr *items;
    out->items = NULL;
    out->count = 0;
    if (!r->ok || n > 4096) return r->ok = 0;
    items = n ? (Attr *)arena_alloc(arena, n * sizeof(Attr)) : NULL;
    if (n && !items) return r->ok = 0;
    for (i = 0; i < n && r->ok; i++) {
        Attr *a = &items[i];
        int k;
        a->key = rd_str(r, arena);
        a->type = rd_u8(r);
        switch (a->type) {
        case AT_INT:
            a->v[0] = (float)(int32_t)rd_u32(r);
            break;
        case AT_FLOAT:
            a->v[0] = rd_f32(r);
            break;
        case AT_BOOL:
            a->v[0] = rd_u8(r) ? 1.0f : 0.0f;
            break;
        case AT_STRING:
            a->s = rd_str(r, arena);
            break;
        case AT_COLOR:
            for (k = 0; k < 4; k++) a->v[k] = (float)rd_u8(r);
            break;
        case AT_VEC2:
        case AT_VEC3:
        case AT_VEC4:
            for (k = 0; k < a->type - AT_VEC2 + 2; k++) a->v[k] = rd_f32(r);
            break;
        default:
            r->ok = 0;
        }
        if (!a->key) r->ok = 0;
    }
    out->items = items;
    out->count = (int)n;
    return r->ok;
}

/* ------------------------------------------------------------------ scenes */

static Scene *scene_get(int id) {
    if (id < 1 || id > MAX_SCENES) return NULL;
    return g_scenes[id - 1];
}

static void scene_clear(Scene *sc) {
    pp_sim_free(sc);
    arena_free(sc->arena);
    free(sc->mat_names);
    free(sc->mat_ids);
    free(sc->issues);
    sc->arena = NULL;
    sc->defs = NULL;
    sc->ndefs = 0;
    sc->mat_names = NULL;
    sc->mat_ids = NULL;
    sc->nmat = 0;
    sc->issues = NULL;
    sc->nissues = 0;
}

PP_API int pp_scene_create(void) {
    int i;
    for (i = 0; i < MAX_SCENES; i++) {
        if (!g_scenes[i]) {
            Scene *sc = (Scene *)calloc(1, sizeof(Scene));
            if (!sc) break;
            sc->used = 1;
            sc->yaw = 35.0f;
            sc->pitch = 20.0f;
            sc->distance = 200.0f;
            sc->fov = 50.0f;
            sc->options = PP_OPT_GRID | PP_OPT_AXES;
            sc->bg_top = 0x2c2f36;
            sc->bg_bottom = 0x121316;
            sc->seed = 1;
            g_scenes[i] = sc;
            return i + 1;
        }
    }
    pp_set_error("too many scenes");
    return 0;
}

PP_API void pp_scene_destroy(int id) {
    Scene *sc = scene_get(id);
    if (!sc) return;
    scene_clear(sc);
    free(sc->acc);
    free(sc);
    g_scenes[id - 1] = NULL;
}

static void normalize_material(char *s) {
    char *w = s, *r = s;
    size_t n;
    for (; *r; r++) *w++ = (char)(*r == '\\' ? '/' : tolower((unsigned char)*r));
    *w = 0;
    if (strncmp(s, "materials/", 10) == 0) memmove(s, s + 10, strlen(s + 10) + 1);
    n = strlen(s);
    if (n >= 4 && strcmp(s + n - 4, ".vmt") == 0) s[n - 4] = 0;
}

static void add_issue(Scene *sc, const char *status, const Func *fn) {
    static const char *const cats[CAT_COUNT] = {"renderer", "operator", "initializer", "emitter", "force", "constraint"};
    char buf[600];
    int i;
    char *copy;
    snprintf(buf, sizeof(buf), "%s|%s|%s", status, cats[fn->category], fn->name ? fn->name : "");
    for (i = 0; i < sc->nissues; i++)
        if (strcmp(sc->issues[i], buf) == 0) return;
    if (sc->nissues >= MAX_ISSUES) return;
    copy = (char *)arena_alloc(&sc->arena, strlen(buf) + 1);
    if (!copy) return;
    strcpy(copy, buf);
    sc->issues[sc->nissues++] = copy;
}

static int load_blob(Scene *sc, const unsigned char *blob, int size, const char *root) {
    Reader r;
    uint32_t ndefs, d, f, c;
    int i;
    r.p = blob;
    r.end = blob + (size > 0 ? size : 0);
    r.ok = 1;
    if (!blob || size < 8 || memcmp(blob, "PPB1", 4) != 0) {
        pp_set_error("bad definition blob");
        return 0;
    }
    r.p += 4;
    ndefs = rd_u32(&r);
    if (!r.ok || ndefs == 0 || ndefs > 4096) {
        pp_set_error("bad definition count");
        return 0;
    }
    sc->defs = (Def *)arena_alloc(&sc->arena, ndefs * sizeof(Def));
    if (!sc->defs) {
        pp_set_error("out of memory");
        return 0;
    }
    sc->ndefs = (int)ndefs;
    for (d = 0; d < ndefs && r.ok; d++) {
        Def *def = &sc->defs[d];
        uint32_t nf, nc;
        def->name = rd_str(&r, &sc->arena);
        rd_attrs(&r, &sc->arena, &def->attrs);
        nf = rd_u32(&r);
        if (!r.ok || nf > 1024) break;
        def->funcs = nf ? (Func *)arena_alloc(&sc->arena, nf * sizeof(Func)) : NULL;
        if (nf && !def->funcs) {
            r.ok = 0;
            break;
        }
        def->nfuncs = (int)nf;
        for (f = 0; f < nf && r.ok; f++) {
            Func *fn = &def->funcs[f];
            fn->category = rd_u8(&r);
            if (fn->category >= CAT_COUNT) r.ok = 0;
            fn->name = rd_str(&r, &sc->arena);
            rd_attrs(&r, &sc->arena, &fn->attrs);
            fn->salt = pp_hash(d * 1024u + f + 17u);
        }
        nc = rd_u32(&r);
        if (!r.ok || nc > 1024) break;
        def->children = nc ? (ChildRef *)arena_alloc(&sc->arena, nc * sizeof(ChildRef)) : NULL;
        if (nc && !def->children) {
            r.ok = 0;
            break;
        }
        def->nchildren = (int)nc;
        for (c = 0; c < nc && r.ok; c++) {
            def->children[c].def = (int)rd_u32(&r);
            def->children[c].delay = rd_f32(&r);
            def->children[c].end_cap = rd_u8(&r);
            if (def->children[c].def < 0 || def->children[c].def >= (int)ndefs) r.ok = 0;
        }
        if (!def->name) r.ok = 0;
    }
    if (!r.ok || d != ndefs) {
        pp_set_error("truncated or corrupt definition blob");
        return 0;
    }
    sc->root_def = 0;
    if (root && root[0]) {
        for (i = 0; i < sc->ndefs; i++) {
            if (strcmp(sc->defs[i].name, root) == 0) {
                sc->root_def = i;
                break;
            }
        }
        if (i == sc->ndefs) {
            pp_set_error("particle system '%s' not in blob", root);
            return 0;
        }
    }
    /* compile, collect materials and issues */
    sc->mat_names = (char **)calloc((size_t)sc->ndefs, sizeof(char *));
    sc->mat_ids = (int *)calloc((size_t)sc->ndefs, sizeof(int));
    sc->issues = (char **)calloc(MAX_ISSUES, sizeof(char *));
    if (!sc->mat_names || !sc->mat_ids || !sc->issues) {
        pp_set_error("out of memory");
        return 0;
    }
    for (i = 0; i < sc->ndefs; i++) {
        Def *def = &sc->defs[i];
        const char *mat = attr_string(&def->attrs, "material", "");
        char *norm;
        int k;
        pp_compile_def(def);
        for (k = 0; k < def->nfuncs; k++) {
            Func *fn = &def->funcs[k];
            pp_compile_func(fn);
            if (fn->status == 1) add_issue(sc, "approximated", fn);
            if (fn->status == 2) add_issue(sc, "not rendered", fn);
            if (fn->status == 3) add_issue(sc, "unsupported", fn);
        }
        def->material = -1;
        norm = (char *)arena_alloc(&sc->arena, strlen(mat) + 1);
        if (!norm) continue;
        strcpy(norm, mat);
        normalize_material(norm);
        if (!norm[0]) continue;
        for (k = 0; k < sc->nmat; k++)
            if (strcmp(sc->mat_names[k], norm) == 0) break;
        if (k == sc->nmat) sc->mat_names[sc->nmat++] = norm;
        def->material = k;
    }
    return 1;
}

PP_API int pp_scene_load(int id, const unsigned char *blob, int size, const char *root) {
    Scene *sc = scene_get(id);
    if (!sc) {
        pp_set_error("invalid scene");
        return 0;
    }
    scene_clear(sc);
    if (!load_blob(sc, blob, size, root)) {
        scene_clear(sc);
        return 0;
    }
    pp_sim_reset(sc);
    if (!sc->systems || sc->nsystems == 0) {
        pp_set_error("could not create the particle system");
        scene_clear(sc);
        return 0;
    }
    return 1;
}

const Material *pp_scene_material(const Scene *sc, int index) {
    if (!sc || index < 0 || index >= sc->nmat) return NULL;
    return pp_material_lookup(sc->mat_ids[index]);
}

PP_API int pp_scene_material_count(int id) {
    Scene *sc = scene_get(id);
    return sc ? sc->nmat : 0;
}

PP_API const char *pp_scene_material_name(int id, int index) {
    Scene *sc = scene_get(id);
    if (!sc || index < 0 || index >= sc->nmat) return "";
    return sc->mat_names[index];
}

PP_API int pp_scene_bind_material(int id, const char *name, int material) {
    Scene *sc = scene_get(id);
    int i;
    if (!sc || !name) return 0;
    for (i = 0; i < sc->nmat; i++) {
        if (strcmp(sc->mat_names[i], name) == 0) {
            sc->mat_ids[i] = material;
            return 1;
        }
    }
    return 0;
}

PP_API int pp_scene_issue_count(int id) {
    Scene *sc = scene_get(id);
    return sc ? sc->nissues : 0;
}

PP_API const char *pp_scene_issue(int id, int index) {
    Scene *sc = scene_get(id);
    if (!sc || index < 0 || index >= sc->nissues) return "";
    return sc->issues[index];
}

PP_API void pp_scene_restart(int id, unsigned int seed) {
    Scene *sc = scene_get(id);
    if (!sc || sc->ndefs <= 0) return;
    sc->seed = seed ? seed : 1;
    pp_sim_reset(sc);
}

PP_API void pp_scene_step(int id, float seconds) {
    Scene *sc = scene_get(id);
    if (sc) pp_sim_step(sc, seconds);
}

PP_API float pp_scene_time(int id) {
    Scene *sc = scene_get(id);
    return sc ? sc->time : 0.0f;
}

PP_API int pp_scene_particle_count(int id) {
    Scene *sc = scene_get(id);
    int i, n = 0;
    if (!sc || !sc->systems) return 0;
    for (i = 0; i < sc->nsystems; i++) n += sc->systems[i].count;
    return n;
}

PP_API int pp_scene_system_count(int id) {
    Scene *sc = scene_get(id);
    return sc ? sc->nsystems : 0;
}

static void sort_floats(float *v, int n) {
    static const int gaps[] = {1750, 701, 301, 132, 57, 23, 10, 4, 1};
    size_t g;
    for (g = 0; g < sizeof(gaps) / sizeof(gaps[0]); g++) {
        int gap = gaps[g], i;
        for (i = gap; i < n; i++) {
            float t = v[i];
            int j = i;
            while (j >= gap && v[j - gap] > t) {
                v[j] = v[j - gap];
                j -= gap;
            }
            v[j] = t;
        }
    }
}

/* Robust extent of the visible particles: 10th..90th percentile per axis plus the
   median radius, so a few stray particles (e.g. a glow that falls forever without
   the world to land on) do not push the camera away. */
PP_API int pp_scene_bounds(int id, float *out) {
    enum { MAX_SAMPLES = 4096 };
    Scene *sc = scene_get(id);
    float *buf;
    int i, k, a, total = 0, n = 0, stride, seen = 0;
    if (!sc || !sc->systems || !out) return 0;
    for (k = 0; k < 6; k++) out[k] = 0.0f;
    for (i = 0; i < sc->nsystems; i++)
        for (k = 0; k < sc->systems[i].count; k++) total += sc->systems[i].alpha[k] > 0.02f;
    if (total == 0) return 0;
    stride = (total + MAX_SAMPLES - 1) / MAX_SAMPLES;
    buf = (float *)malloc(sizeof(float) * 4 * MAX_SAMPLES);
    if (!buf) return 0;
    for (i = 0; i < sc->nsystems && n < MAX_SAMPLES; i++) {
        const System *s = &sc->systems[i];
        for (k = 0; k < s->count && n < MAX_SAMPLES; k++) {
            if (!(s->alpha[k] > 0.02f)) continue;
            if (seen++ % stride) continue;
            for (a = 0; a < 3; a++) buf[a * MAX_SAMPLES + n] = s->pos[3 * k + a];
            buf[3 * MAX_SAMPLES + n] = fminf(fabsf(s->radius[k]), 1e5f);
            n++;
        }
    }
    for (a = 0; a < 4; a++) sort_floats(buf + a * MAX_SAMPLES, n);
    {
        int lo = n >= 10 ? n / 10 : 0, hi = n >= 10 ? n - 1 - n / 10 : n - 1;
        float r = buf[3 * MAX_SAMPLES + n / 2];
        for (a = 0; a < 3; a++) {
            out[a] = buf[a * MAX_SAMPLES + lo] - r;
            out[a + 3] = buf[a * MAX_SAMPLES + hi] + r;
        }
    }
    free(buf);
    return 1;
}

PP_API void pp_scene_set_camera(int id, float yaw, float pitch, float distance, float tx, float ty, float tz, float fov) {
    Scene *sc = scene_get(id);
    if (!sc) return;
    if (finitef(yaw)) sc->yaw = yaw;
    if (finitef(pitch)) sc->pitch = clampf(pitch, -89.0f, 89.0f);
    if (finitef(distance)) sc->distance = clampf(distance, 1.0f, 100000.0f);
    if (finitef(tx) && finitef(ty) && finitef(tz)) sc->target = v3(tx, ty, tz);
    if (finitef(fov)) sc->fov = clampf(fov, 5.0f, 150.0f);
}

PP_API void pp_scene_set_options(int id, int flags, unsigned int top, unsigned int bottom) {
    Scene *sc = scene_get(id);
    if (!sc) return;
    sc->options = flags;
    sc->bg_top = top & 0xffffff;
    sc->bg_bottom = bottom & 0xffffff;
}

PP_API void pp_scene_set_control_point(int id, int index, float x, float y, float z) {
    Scene *sc = scene_get(id);
    if (!sc || index < 0 || index >= PP_MAX_CPS || !finitef(x) || !finitef(y) || !finitef(z)) return;
    sc->cp[index] = v3(x, y, z);
}

PP_API int pp_scene_render(int id, unsigned char *out, int width, int height, int stride) {
    Scene *sc = scene_get(id);
    if (!sc) {
        pp_set_error("invalid scene");
        return 0;
    }
    return pp_render_scene(sc, out, width, height, stride);
}

PP_API int pp_set_render_threads(int count) { return pp_render_set_threads(count); }

PP_API int pp_render_thread_count(void) { return pp_render_threads(); }

PP_API int pp_scene_particle_capacity(int id) {
    Scene *sc = scene_get(id);
    return sc ? sc->total_cap : 0;
}

/* ------------------------------------------------------------------ self test */

static void put_u32(unsigned char **p, uint32_t v) {
    (*p)[0] = (unsigned char)v;
    (*p)[1] = (unsigned char)(v >> 8);
    (*p)[2] = (unsigned char)(v >> 16);
    (*p)[3] = (unsigned char)(v >> 24);
    *p += 4;
}

static void put_str(unsigned char **p, const char *s) {
    size_t n = strlen(s);
    (*p)[0] = (unsigned char)n;
    (*p)[1] = (unsigned char)(n >> 8);
    memcpy(*p + 2, s, n);
    *p += 2 + n;
}

static void put_int_attr(unsigned char **p, const char *key, int v) {
    put_str(p, key);
    *(*p)++ = AT_INT;
    put_u32(p, (uint32_t)v);
}

PP_API int pp_self_test(void) {
    unsigned char blob[512], *p = blob, px[64 * 64 * 4];
    int scene, ok = 0, i, lit = 0;
    memcpy(p, "PPB1", 4);
    p += 4;
    put_u32(&p, 1);
    put_str(&p, "self_test");
    put_u32(&p, 1);
    put_int_attr(&p, "max_particles", 16);
    put_u32(&p, 2);
    *p++ = CAT_EMITTER;
    put_str(&p, "emit_instantaneously");
    put_u32(&p, 1);
    put_int_attr(&p, "num_to_emit", 8);
    *p++ = CAT_RENDERER;
    put_str(&p, "render_animated_sprites");
    put_u32(&p, 0);
    put_u32(&p, 0);
    scene = pp_scene_create();
    if (!scene) return 0;
    if (pp_scene_load(scene, blob, (int)(p - blob), "self_test")) {
        pp_scene_set_options(scene, 0, 0, 0);
        pp_scene_set_camera(scene, 0.0f, 0.0f, 40.0f, 0.0f, 0.0f, 0.0f, 50.0f);
        pp_scene_step(scene, 0.05f);
        if (pp_scene_particle_count(scene) == 8 && pp_scene_render(scene, px, 64, 64, 64 * 4)) {
            for (i = 0; i < 64 * 64; i++) lit += px[i * 4 + 1] > 32;
            ok = lit > 16;
            if (!ok) pp_set_error("self test rendered nothing (lit pixels %d, centre %d/%d/%d)", lit, px[(32 * 64 + 32) * 4 + 2],
                                  px[(32 * 64 + 32) * 4 + 1], px[(32 * 64 + 32) * 4]);
            if (ok && pp_render_threads() > 1) {
                /* the threaded path must give the same frame; if not, stay single-threaded */
                static unsigned char threaded[64 * 64 * 4];
                pp_force_threads = 1;
                pp_scene_render(scene, threaded, 64, 64, 64 * 4);
                pp_force_threads = 0;
                if (memcmp(px, threaded, sizeof(threaded)) != 0) pp_render_set_threads(1);
            }
        } else if (!g_error[0]) {
            pp_set_error("self test simulation failed (particles %d of 8)", pp_scene_particle_count(scene));
        }
    }
    pp_scene_destroy(scene);
    return ok;
}
