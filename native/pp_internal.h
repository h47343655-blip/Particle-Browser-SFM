#ifndef PP_INTERNAL_H
#define PP_INTERNAL_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "pp_render.h"

#define PP_PI 3.14159265358979323846f
#define PP_DEG 0.017453292519943295f

/* Hard limits keep hostile or broken input from exhausting memory. */
#define PP_MAX_PARTICLES 16384      /* per system */
#define PP_MAX_SCENE_PARTICLES 262144
#define PP_MAX_SYSTEMS 512          /* per scene, children included */
#define PP_MAX_DEPTH 12
#define PP_MAX_CPS 16
#define PP_MAX_SEQUENCES 256
#define PP_MAX_MIPS 16
#define PP_MAX_TEXTURE_DIM 512      /* larger mips are dropped: SFM is a 32-bit process */
#define PP_MAX_RENDER_DIM 4096

void pp_set_error(const char *fmt, ...);

/* ------------------------------------------------------------------ math */

typedef struct {
    float x, y, z;
} Vec3;

static inline Vec3 v3(float x, float y, float z) {
    Vec3 r;
    r.x = x;
    r.y = y;
    r.z = z;
    return r;
}
static inline Vec3 v3add(Vec3 a, Vec3 b) { return v3(a.x + b.x, a.y + b.y, a.z + b.z); }
static inline Vec3 v3sub(Vec3 a, Vec3 b) { return v3(a.x - b.x, a.y - b.y, a.z - b.z); }
static inline Vec3 v3scale(Vec3 a, float s) { return v3(a.x * s, a.y * s, a.z * s); }
static inline Vec3 v3mul(Vec3 a, Vec3 b) { return v3(a.x * b.x, a.y * b.y, a.z * b.z); }
static inline float v3dot(Vec3 a, Vec3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
static inline Vec3 v3cross(Vec3 a, Vec3 b) {
    return v3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
}
static inline float v3len(Vec3 a) { return sqrtf(v3dot(a, a)); }
static inline Vec3 v3norm(Vec3 a, Vec3 fallback) {
    float l = v3len(a);
    return l > 1e-12f ? v3scale(a, 1.0f / l) : fallback;
}
static inline Vec3 v3lerp(Vec3 a, Vec3 b, float t) {
    return v3(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t);
}
static inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
static inline float saturatef(float v) { return clampf(v, 0.0f, 1.0f); }
static inline float lerpf(float a, float b, float t) { return a + (b - a) * t; }
static inline float smoothstepf(float t) {
    t = saturatef(t);
    return t * t * (3.0f - 2.0f * t);
}
/* inverse lerp that tolerates an empty range (a step at a) */
static inline float invlerpf(float a, float b, float v) {
    if (b - a > 1e-12f || a - b > 1e-12f) return (v - a) / (b - a);
    return v >= a ? 1.0f : 0.0f;
}
static inline int finitef(float v) { return v == v && v < 3.0e38f && v > -3.0e38f; }
/* float -> int clamped to [lo, hi]; NaN maps to lo (a plain cast of NaN or huge values is UB) */
static inline int clamp_int(float v, int lo, int hi) {
    if (!(v > (float)lo)) return lo;
    if (!(v < (float)hi)) return hi;
    return (int)v;
}
static inline int seq_index(float v) { return clamp_int(v, -1, 1 << 20); }

/* sin/cos without libm: the same bits on every platform, and no x87 code paths
   (the 32-bit mingw libm uses fsincos). */
void pp_sincos(float angle, float *s, float *c);
static inline float pp_cos(float angle) {
    float s, c;
    pp_sincos(angle, &s, &c);
    return c;
}

/* ------------------------------------------------------------------ random */

static inline uint32_t pp_hash(uint32_t x) {
    x ^= x >> 16;
    x *= 0x7feb352dU;
    x ^= x >> 15;
    x *= 0x846ca68bU;
    x ^= x >> 16;
    return x;
}
typedef struct {
    uint32_t s;
} Rng;
static inline uint32_t rng_u32(Rng *r) {
    uint32_t x = r->s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    r->s = x ? x : 0x9e3779b9U;
    return r->s;
}
static inline float rng_f(Rng *r) { return (float)(rng_u32(r) >> 8) * (1.0f / 16777216.0f); }
static inline float rng_range(Rng *r, float a, float b) { return a + (b - a) * rng_f(r); }
/* deterministic per-particle random value for operator "salt" */
static inline float hash_f(uint32_t seed, uint32_t salt) {
    return (float)(pp_hash(seed ^ pp_hash(salt + 0x9e3779b9U)) >> 8) * (1.0f / 16777216.0f);
}
static inline float range_exp(float lo, float hi, float exponent, float u) {
    if (lo == hi) return lo;
    if (exponent != 1.0f && exponent > 0.0f) u = powf(u, exponent);
    return lerpf(lo, hi, u);
}

/* ------------------------------------------------------------------ attributes */

enum { AT_INT = 1, AT_FLOAT = 2, AT_BOOL = 3, AT_STRING = 4, AT_COLOR = 5, AT_VEC2 = 6, AT_VEC3 = 7, AT_VEC4 = 8 };

typedef struct {
    const char *key;
    int type;
    float v[4];
    const char *s;
} Attr;

typedef struct {
    const Attr *items;
    int count;
} AttrList;

const Attr *attr_find(const AttrList *list, const char *key);
float attr_float(const AttrList *list, const char *key, float def);
int attr_int(const AttrList *list, const char *key, int def);
int attr_bool(const AttrList *list, const char *key, int def);
Vec3 attr_vec3(const AttrList *list, const char *key, Vec3 def);
void attr_color(const AttrList *list, const char *key, float out[4], float r, float g, float b, float a);
const char *attr_string(const AttrList *list, const char *key, const char *def);

/* ------------------------------------------------------------------ materials */

typedef struct {
    float u0, v0, u1, v1, duration;
} SheetFrame;

typedef struct {
    int clamp;
    int count;
    float duration;
    SheetFrame *frames;
} SheetSeq;

typedef struct {
    int width, height; /* base level */
    int mips;
    int mip_w[PP_MAX_MIPS], mip_h[PP_MAX_MIPS];
    uint8_t *mip_data[PP_MAX_MIPS]; /* premultiplied RGBA8 */
    int flags;
    float overbright;
    int nseq;       /* sequence numbers are 0..nseq-1 */
    SheetSeq *seqs; /* NULL when the texture has no sheet */
} Material;

int pp_vtf_decode(const uint8_t *data, int size, Material *out);
int pp_material_from_rgba(const uint8_t *rgba, int width, int height, Material *out);
void pp_material_release(Material *m);
Material *pp_material_lookup(int id);
const Material *pp_default_material(void);
/* frame lookup: returns blend weight towards frame b */
float pp_sheet_frames(const Material *m, int seq, float phase, const SheetFrame **a, const SheetFrame **b);
int pp_sheet_frame_count(const Material *m, int seq);

/* ------------------------------------------------------------------ definitions */

enum { CAT_RENDERER = 0, CAT_OPERATOR, CAT_INITIALIZER, CAT_EMITTER, CAT_FORCE, CAT_CONSTRAINT, CAT_COUNT };

enum {
    K_UNKNOWN = 0,
    K_IGNORED,
    /* emitters */
    K_EMIT_CONTINUOUS,
    K_EMIT_INSTANT,
    /* initializers */
    K_INIT_LIFETIME,
    K_INIT_RADIUS,
    K_INIT_ALPHA,
    K_INIT_TRAIL,
    K_INIT_ROTATION,
    K_INIT_YAW,
    K_INIT_ROTATION_SPEED,
    K_INIT_YAW_FLIP,
    K_INIT_COLOR,
    K_INIT_SEQUENCE,
    K_INIT_SEQUENCE2,
    K_INIT_POS_SPHERE,
    K_INIT_POS_BOX,
    K_INIT_POS_OFFSET,
    K_INIT_POS_AT_CP,
    K_INIT_POS_PATH,
    K_INIT_POS_FROM_PARENT,
    K_INIT_VELOCITY_RANDOM,
    K_INIT_VELOCITY_NOISE,
    K_INIT_LIFETIME_FROM_SEQ,
    K_INIT_REMAP_INITIAL_SCALAR,
    K_INIT_NOISE_SCALAR,
    K_INIT_REMAP_CP_SCALAR,
    K_INIT_PRE_AGE,
    /* operators */
    K_OP_MOVEMENT_BASIC,
    K_OP_LIFESPAN_DECAY,
    K_OP_ALPHA_FADE_DECAY,
    K_OP_ALPHA_FADE_IN_RANDOM,
    K_OP_ALPHA_FADE_OUT_RANDOM,
    K_OP_ALPHA_FADE_IN_SIMPLE,
    K_OP_ALPHA_FADE_OUT_SIMPLE,
    K_OP_RADIUS_SCALE,
    K_OP_COLOR_FADE,
    K_OP_ROTATION_BASIC,
    K_OP_SPIN_ROLL,
    K_OP_SPIN_YAW,
    K_OP_OSCILLATE_SCALAR,
    K_OP_OSCILLATE_VECTOR,
    K_OP_DAMPEN_TO_CP,
    K_OP_ROTATE_AROUND_AXIS,
    K_OP_REMAP_DIST_TO_CP,
    K_OP_REMAP_SCALAR,
    K_OP_REMAP_CP_SCALAR,
    K_OP_REMAP_SPEED,
    K_OP_SET_CP_POSITIONS,
    K_OP_NOOP,
    /* forces */
    K_FORCE_RANDOM,
    K_FORCE_TWIST,
    K_FORCE_PULL,
    K_FORCE_TURBULENT,
    /* constraints */
    K_CON_DIST_TO_CP,
    K_CON_PLANE,
    /* renderers */
    K_REN_SPRITES,
    K_REN_TRAIL,
    K_REN_ROPE,
    K_REN_VELOCITY_ROTATE,
    K_KIND_COUNT
};

/* field indices as stored in PCF files (PARTICLE_ATTRIBUTE_*) */
enum {
    F_XYZ = 0,
    F_LIFE = 1,
    F_PREV_XYZ = 2,
    F_RADIUS = 3,
    F_ROTATION = 4,
    F_ROTATION_SPEED = 5,
    F_TINT = 6,
    F_ALPHA = 7,
    F_CREATION = 8,
    F_SEQUENCE = 9,
    F_TRAIL = 10,
    F_ID = 11,
    F_YAW = 12,
    F_SEQUENCE2 = 13,
    F_ALPHA2 = 16
};

typedef struct {
    int category;
    int kind;
    int status; /* 0 supported, 1 approximated, 2 not rendered, 3 unknown */
    const char *name;
    AttrList attrs;
    uint32_t salt;
    float weight_in0, weight_in1, weight_out0, weight_out1, weight_osc;
    /* compiled parameters; meaning depends on kind (see pp_defs.c) */
    float f[20];
    Vec3 v[6];
    int i[8];
} Func;

typedef struct {
    int def;
    float delay;
    int end_cap;
} ChildRef;

typedef struct {
    const char *name;
    AttrList attrs;
    Func *funcs;
    int nfuncs;
    ChildRef *children;
    int nchildren;
    /* compiled */
    int max_particles;
    int initial_particles;
    float radius;
    float color[4];
    float rotation;
    float rotation_speed;
    int sequence;
    int sequence2;
    int sort;
    int material; /* index into Scene.mat_names */
} Def;

void pp_compile_func(Func *func);
void pp_compile_def(Def *def);
const char *pp_canonical_name(const char *name);

/* ------------------------------------------------------------------ runtime */

typedef struct System {
    const Def *def;
    struct System *parent;
    float start; /* scene time when the system starts */
    float time;  /* system age */
    int started;
    int count, cap;
    float *pos, *vel, *life, *spawn, *radius, *radius0, *alpha, *alpha0, *color, *color0;
    float *rot, *rotspeed, *yaw, *seq, *seq2, *trail, *alpha2;
    uint32_t *seed;
    uint8_t *dead;
    void *block;
    float *emit_acc;
    int *emit_done;
    int parent_cursor;
    int next_id;
    Vec3 cp[PP_MAX_CPS];
    int nchildren;
    struct System **children;
} System;

typedef struct Arena Arena;

typedef struct {
    int used;
    Arena *arena;
    Def *defs;
    int ndefs;
    int root_def;
    char **mat_names;
    int *mat_ids;
    int nmat;
    char **issues;
    int nissues;
    /* runtime */
    Rng rng;
    unsigned int seed;
    float time;
    System *systems;
    int nsystems;
    int total_cap;
    Vec3 cp[PP_MAX_CPS];
    /* camera */
    float yaw, pitch, distance, fov;
    Vec3 target;
    int options;
    uint32_t bg_top, bg_bottom;
    float *acc;
    int acc_len;
    void *items; /* render scratch (pp_raster.c), freed by pp_sim_free */
    int items_cap;
    void *cmds; /* recorded draw commands for threaded rendering (pp_raster.c) */
    int cmds_cap;
} Scene;

void pp_sim_reset(Scene *scene);
void pp_sim_free(Scene *scene);
void pp_sim_step(Scene *scene, float dt);
const Material *pp_scene_material(const Scene *scene, int index);
int pp_render_scene(Scene *scene, unsigned char *out, int width, int height, int stride);

/* render threads (pp_raster.c): 0 = automatic, 1 = single-threaded */
int pp_render_set_threads(int count);
int pp_render_threads(void);
/* test hook: 1 forces the threaded path even for small frames */
extern int pp_force_threads;

#endif
