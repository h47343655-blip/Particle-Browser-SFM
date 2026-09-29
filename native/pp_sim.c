/* Particle simulation: emitters, initializers, operators, forces and constraints. */
#include "pp_internal.h"

#define FLOATS_PER_PARTICLE 25

/* ------------------------------------------------------------------ fields */

static int rotation_field(int field) { return field == F_ROTATION || field == F_YAW || field == F_ROTATION_SPEED; }

static float *field_ptr(System *s, int i, int field) {
    switch (field) {
    case F_LIFE: return &s->life[i];
    case F_RADIUS: return &s->radius[i];
    case F_ROTATION: return &s->rot[i];
    case F_ROTATION_SPEED: return &s->rotspeed[i];
    case F_ALPHA: return &s->alpha[i];
    case F_CREATION: return &s->spawn[i];
    case F_SEQUENCE: return &s->seq[i];
    case F_TRAIL: return &s->trail[i];
    case F_YAW: return &s->yaw[i];
    case F_SEQUENCE2: return &s->seq2[i];
    case F_ALPHA2: return &s->alpha2[i];
    default: return NULL;
    }
}

static float field_get(System *s, int i, int field) {
    float *p = field_ptr(s, i, field);
    if (!p) return 0.0f;
    return rotation_field(field) ? *p / PP_DEG : *p;
}

/* PCF values for rotation fields are in degrees */
static void field_set(System *s, int i, int field, float value, int scale_initial) {
    float *p = field_ptr(s, i, field);
    if (!p || !finitef(value)) return;
    if (scale_initial) {
        float base = field == F_RADIUS ? s->radius0[i] : (field == F_ALPHA ? s->alpha0[i] : *p);
        *p = base * value;
        return;
    }
    *p = rotation_field(field) ? value * PP_DEG : value;
}

/* ------------------------------------------------------------------ helpers */

static Vec3 rand_unit(Rng *r) {
    float z = rng_range(r, -1.0f, 1.0f), a = rng_range(r, 0.0f, 2.0f * PP_PI), s, c;
    float q = sqrtf(fmaxf(1.0f - z * z, 0.0f));
    pp_sincos(a, &s, &c);
    return v3(q * c, q * s, z);
}

static Vec3 rand_box(Rng *r, Vec3 lo, Vec3 hi) {
    return v3(lo.x == hi.x ? lo.x : rng_range(r, lo.x, hi.x), lo.y == hi.y ? lo.y : rng_range(r, lo.y, hi.y),
              lo.z == hi.z ? lo.z : rng_range(r, lo.z, hi.z));
}

static float func_weight(const Func *fn, float t) {
    float w = 1.0f;
    if (fn->weight_osc > 0.0f) t = fmodf(t / fn->weight_osc, 1.0f);
    if (fn->weight_in1 > fn->weight_in0) {
        if (t < fn->weight_in0) return 0.0f;
        if (t < fn->weight_in1) w = (t - fn->weight_in0) / (fn->weight_in1 - fn->weight_in0);
    }
    if (fn->weight_out1 > fn->weight_out0) {
        if (t > fn->weight_out1) return 0.0f;
        if (t > fn->weight_out0) w *= 1.0f - (t - fn->weight_out0) / (fn->weight_out1 - fn->weight_out0);
    }
    return w;
}

static Vec3 cp_get(const System *s, int cp) { return s->cp[cp >= 0 && cp < PP_MAX_CPS ? cp : 0]; }

static float schlick_bias(float t, float bias) { return t / ((1.0f / bias - 2.0f) * (1.0f - t) + 1.0f); }

static Vec3 rotate_axis(Vec3 v, Vec3 axis, float angle) {
    float c, s;
    pp_sincos(angle, &s, &c);
    return v3add(v3add(v3scale(v, c), v3scale(v3cross(axis, v), s)), v3scale(axis, v3dot(axis, v) * (1.0f - c)));
}

/* ------------------------------------------------------------------ systems */

static void system_free(System *s) {
    free(s->block);
    free(s->emit_acc);
    free(s->emit_done);
    free(s->children);
    memset(s, 0, sizeof(*s));
}

static int system_alloc(System *s, int cap, int nfuncs) {
    size_t n = (size_t)cap, bytes = n * (FLOATS_PER_PARTICLE * sizeof(float) + sizeof(uint32_t) + 1);
    float *f;
    s->block = calloc(1, bytes ? bytes : 1);
    s->emit_acc = (float *)calloc((size_t)nfuncs + 1, sizeof(float));
    s->emit_done = (int *)calloc((size_t)nfuncs + 1, sizeof(int));
    if (!s->block || !s->emit_acc || !s->emit_done) return 0;
    f = (float *)s->block;
    s->pos = f, f += 3 * n;
    s->vel = f, f += 3 * n;
    s->color = f, f += 3 * n;
    s->color0 = f, f += 3 * n;
    s->life = f, f += n;
    s->spawn = f, f += n;
    s->radius = f, f += n;
    s->radius0 = f, f += n;
    s->alpha = f, f += n;
    s->alpha0 = f, f += n;
    s->rot = f, f += n;
    s->rotspeed = f, f += n;
    s->yaw = f, f += n;
    s->seq = f, f += n;
    s->seq2 = f, f += n;
    s->trail = f, f += n;
    s->alpha2 = f, f += n;
    s->seed = (uint32_t *)f;
    s->dead = (uint8_t *)(s->seed + n);
    s->cap = cap;
    return 1;
}

static System *create_system(Scene *sc, int def_index, System *parent, float start, int depth) {
    const Def *d;
    System *s;
    int cap, k, n = 0;
    if (def_index < 0 || def_index >= sc->ndefs || sc->nsystems >= PP_MAX_SYSTEMS || depth > PP_MAX_DEPTH) return NULL;
    d = &sc->defs[def_index];
    s = &sc->systems[sc->nsystems++];
    memset(s, 0, sizeof(*s));
    cap = d->max_particles;
    if (sc->total_cap + cap > PP_MAX_SCENE_PARTICLES) cap = PP_MAX_SCENE_PARTICLES - sc->total_cap;
    if (cap < 1) cap = 1;
    sc->total_cap += cap;
    if (!system_alloc(s, cap, d->nfuncs)) {
        system_free(s);
        sc->nsystems--;
        sc->total_cap -= cap;
        return NULL;
    }
    s->def = d;
    s->parent = parent;
    s->start = start;
    for (k = 0; k < d->nfuncs; k++) s->emit_acc[k] = -1.0f;
    memcpy(s->cp, parent ? parent->cp : sc->cp, sizeof(s->cp));
    if (d->nchildren > 0) {
        s->children = (System **)calloc((size_t)d->nchildren, sizeof(System *));
        if (!s->children) return s;
        for (k = 0; k < d->nchildren; k++) {
            System *c;
            if (d->children[k].end_cap) continue;
            c = create_system(sc, d->children[k].def, s, start + (d->children[k].delay > 0 ? d->children[k].delay : 0.0f),
                              depth + 1);
            if (c) s->children[n++] = c;
        }
        s->nchildren = n;
    }
    return s;
}

void pp_sim_free(Scene *sc) {
    int i;
    if (sc->systems) {
        for (i = 0; i < sc->nsystems; i++) system_free(&sc->systems[i]);
        free(sc->systems);
    }
    sc->systems = NULL;
    sc->nsystems = 0;
    sc->total_cap = 0;
    free(sc->items);
    sc->items = NULL;
    sc->items_cap = 0;
    free(sc->cmds);
    sc->cmds = NULL;
    sc->cmds_cap = 0;
}

void pp_sim_reset(Scene *sc) {
    pp_sim_free(sc);
    sc->time = 0.0f;
    sc->rng.s = sc->seed ? sc->seed : 0x12345678U;
    if (sc->ndefs <= 0) return;
    sc->systems = (System *)calloc(PP_MAX_SYSTEMS, sizeof(System));
    if (!sc->systems) return;
    create_system(sc, sc->root_def, NULL, 0.0f, 0);
}

/* ------------------------------------------------------------------ spawning */

static int spawn(Scene *sc, System *s, int n, float t0, float t1) {
    const Def *d = s->def;
    int k, made = 0;
    for (k = 0; k < n && s->count < s->cap; k++) {
        int i = s->count++;
        float t = n > 1 ? lerpf(t0, t1, (k + 1.0f) / (float)n) : t1;
        s->pos[3 * i] = s->cp[0].x;
        s->pos[3 * i + 1] = s->cp[0].y;
        s->pos[3 * i + 2] = s->cp[0].z;
        s->vel[3 * i] = s->vel[3 * i + 1] = s->vel[3 * i + 2] = 0.0f;
        s->color[3 * i] = d->color[0];
        s->color[3 * i + 1] = d->color[1];
        s->color[3 * i + 2] = d->color[2];
        s->life[i] = 1.0f;
        s->spawn[i] = t;
        s->radius[i] = d->radius;
        s->alpha[i] = d->color[3];
        s->rot[i] = d->rotation;
        s->rotspeed[i] = d->rotation_speed;
        s->yaw[i] = 0.0f;
        s->seq[i] = (float)d->sequence;
        s->seq2[i] = (float)d->sequence2;
        s->trail[i] = 0.1f;
        s->alpha2[i] = 1.0f;
        s->seed[i] = rng_u32(&sc->rng);
        s->dead[i] = 0;
        made++;
    }
    return made;
}

static void run_emitters(Scene *sc, System *s, float age0, float age1) {
    int f;
    for (f = 0; f < s->def->nfuncs; f++) {
        const Func *fn = &s->def->funcs[f];
        if (fn->category != CAT_EMITTER) continue;
        if (fn->kind == K_EMIT_CONTINUOUS) {
            float rate = fn->f[0], a0 = age0 > fn->f[2] ? age0 : fn->f[2], a1 = age1;
            int n;
            if (fn->f[1] > 0.0f && a1 > fn->f[2] + fn->f[1]) a1 = fn->f[2] + fn->f[1];
            if (!(a1 > a0) || !(rate > 0.0f)) continue;
            if (s->emit_acc[f] < 0.0f) s->emit_acc[f] = 0.0f;
            s->emit_acc[f] += rate * (a1 - a0) * func_weight(fn, age1);
            if (s->emit_acc[f] > (float)s->cap) s->emit_acc[f] = (float)s->cap;
            n = (int)s->emit_acc[f];
            s->emit_acc[f] -= (float)n;
            spawn(sc, s, n, a0, a1);
        } else if (fn->kind == K_EMIT_INSTANT) {
            float start = fn->f[0];
            int total, n;
            if (age1 < start) continue;
            if (s->emit_acc[f] < 0.0f) {
                int hi = fn->i[0], lo = fn->i[1];
                if (lo >= 0) {
                    if (lo > hi) {
                        int t = lo;
                        lo = hi;
                        hi = t;
                    }
                    total = lo + (int)(rng_f(&sc->rng) * (float)(hi - lo + 1));
                    if (total > hi) total = hi;
                } else {
                    total = hi;
                }
                s->emit_acc[f] = (float)(total > 0 ? total : 0);
            }
            total = (int)s->emit_acc[f];
            n = total - s->emit_done[f];
            if (fn->i[2] > 0 && n > fn->i[2]) n = fn->i[2];
            if (n <= 0) continue;
            {
                float t = start > age0 ? start : age1;
                s->emit_done[f] += n;
                spawn(sc, s, n, t, t);
            }
        }
    }
}

/* ------------------------------------------------------------------ initializers */

static void run_initializers(Scene *sc, System *s, int a, int b) {
    const Def *d = s->def;
    const Material *mat = pp_scene_material(sc, d->material);
    Rng *r = &sc->rng;
    int f, i;
    for (f = 0; f < d->nfuncs; f++) {
        const Func *fn = &d->funcs[f];
        if (fn->category != CAT_INITIALIZER) continue;
        for (i = a; i < b; i++) {
            float *p = &s->pos[3 * i], *v = &s->vel[3 * i];
            switch (fn->kind) {
            case K_INIT_LIFETIME:
                s->life[i] = range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                break;
            case K_INIT_RADIUS:
                s->radius[i] = range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                break;
            case K_INIT_ALPHA:
                s->alpha[i] = range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                break;
            case K_INIT_TRAIL:
                s->trail[i] = range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                break;
            case K_INIT_ROTATION:
                s->rot[i] = fn->f[3] + range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                break;
            case K_INIT_YAW:
                s->yaw[i] = fn->f[3] + range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                break;
            case K_INIT_ROTATION_SPEED: {
                float sp = fn->f[3] + range_exp(fn->f[0], fn->f[1], fn->f[2], rng_f(r));
                if (fn->i[0] && rng_f(r) < 0.5f) sp = -sp;
                s->rotspeed[i] = sp;
                break;
            }
            case K_INIT_YAW_FLIP:
                if (rng_f(r) < fn->f[0]) s->yaw[i] += PP_PI;
                break;
            case K_INIT_COLOR: {
                Vec3 c = v3lerp(fn->v[0], fn->v[1], rng_f(r));
                s->color[3 * i] = c.x;
                s->color[3 * i + 1] = c.y;
                s->color[3 * i + 2] = c.z;
                break;
            }
            case K_INIT_SEQUENCE:
            case K_INIT_SEQUENCE2: {
                int lo = fn->i[0], hi = fn->i[1], q;
                if (lo > hi) {
                    int t = lo;
                    lo = hi;
                    hi = t;
                }
                q = lo + (int)(rng_f(r) * (float)(hi - lo + 1));
                if (q > hi) q = hi;
                if (fn->kind == K_INIT_SEQUENCE)
                    s->seq[i] = (float)q;
                else
                    s->seq2[i] = (float)q;
                break;
            }
            case K_INIT_POS_SPHERE: {
                Vec3 dir = v3mul(rand_unit(r), fn->v[0]), cp = cp_get(s, fn->i[0]), vel;
                float dist, lo = fn->f[0], hi = fn->f[1];
                if (fn->v[1].x != 0.0f) dir.x = fabsf(dir.x);
                if (fn->v[1].y != 0.0f) dir.y = fabsf(dir.y);
                if (fn->v[1].z != 0.0f) dir.z = fabsf(dir.z);
                dir = v3norm(dir, v3(0, 0, 1));
                if (lo == hi) {
                    dist = lo;
                } else {
                    /* uniform in the shell volume */
                    float l3 = lo * lo * lo, h3 = hi * hi * hi, u = lerpf(l3, h3, rng_f(r));
                    dist = u >= 0 ? cbrtf(u) : -cbrtf(-u);
                }
                p[0] = cp.x + dir.x * dist;
                p[1] = cp.y + dir.y * dist;
                p[2] = cp.z + dir.z * dist;
                vel = v3add(v3scale(dir, range_exp(fn->f[2], fn->f[3], fn->f[4], rng_f(r))), rand_box(r, fn->v[2], fn->v[3]));
                v[0] = vel.x;
                v[1] = vel.y;
                v[2] = vel.z;
                break;
            }
            case K_INIT_POS_BOX: {
                Vec3 o = v3add(cp_get(s, fn->i[0]), rand_box(r, fn->v[0], fn->v[1]));
                p[0] = o.x;
                p[1] = o.y;
                p[2] = o.z;
                break;
            }
            case K_INIT_POS_OFFSET: {
                Vec3 o = rand_box(r, fn->v[0], fn->v[1]);
                if (fn->i[0]) o = v3scale(o, s->radius[i]);
                p[0] += o.x;
                p[1] += o.y;
                p[2] += o.z;
                break;
            }
            case K_INIT_POS_AT_CP: {
                Vec3 cp = cp_get(s, fn->i[0]);
                p[0] = cp.x;
                p[1] = cp.y;
                p[2] = cp.z;
                break;
            }
            case K_INIT_POS_PATH: {
                /* a point on the line between two control points: sequential particles walk
                   along it (loop or bounce), random ones land anywhere; bulge lifts the middle */
                Vec3 pa = cp_get(s, fn->i[0]), pb = cp_get(s, fn->i[1]), q;
                float t, lift, mid = fn->f[1];
                if (fn->i[2]) {
                    int n = fn->i[3], period = fn->i[4] ? n : 2 * (n - 1), k = s->emit_done[f];
                    if (k < 0 || k >= period) k = 0;
                    s->emit_done[f] = (k + 1) % period; /* emitters use this slot, initializers never do */
                    if (k > n - 1) k = period - k;
                    t = (float)k / (float)(n - 1);
                } else {
                    t = rng_f(r);
                }
                q = v3lerp(pa, pb, t);
                lift = fn->f[0] * (t <= mid ? t / mid : (1.0f - t) / (1.0f - mid));
                q.z += lift; /* approximated: the Particle Editor can bend it towards other directions */
                if (fn->f[2] > 0.0f) q = v3add(q, v3scale(rand_unit(r), fn->f[2] * cbrtf(rng_f(r))));
                p[0] = q.x;
                p[1] = q.y;
                p[2] = q.z;
                break;
            }
            case K_INIT_POS_FROM_PARENT: {
                System *par = s->parent;
                if (par && par->count > 0) {
                    int j;
                    if (fn->i[0]) {
                        j = (int)(rng_f(r) * (float)par->count);
                    } else {
                        j = s->parent_cursor++ % par->count;
                    }
                    if (j >= par->count) j = par->count - 1;
                    p[0] = par->pos[3 * j];
                    p[1] = par->pos[3 * j + 1];
                    p[2] = par->pos[3 * j + 2];
                    v[0] = par->vel[3 * j] * fn->f[0];
                    v[1] = par->vel[3 * j + 1] * fn->f[0];
                    v[2] = par->vel[3 * j + 2] * fn->f[0];
                }
                break;
            }
            case K_INIT_VELOCITY_RANDOM: {
                Vec3 add = v3add(v3scale(rand_unit(r), rng_range(r, fn->f[0], fn->f[1])), rand_box(r, fn->v[0], fn->v[1]));
                v[0] += add.x;
                v[1] += add.y;
                v[2] += add.z;
                break;
            }
            case K_INIT_VELOCITY_NOISE: {
                Vec3 add = rand_box(r, fn->v[0], fn->v[1]);
                v[0] += add.x;
                v[1] += add.y;
                v[2] += add.z;
                break;
            }
            case K_INIT_LIFETIME_FROM_SEQ: {
                int frames = pp_sheet_frame_count(mat, seq_index(s->seq[i]));
                if (frames > 0 && fn->f[0] > 0.0f) s->life[i] = (float)frames / fn->f[0];
                break;
            }
            case K_INIT_REMAP_INITIAL_SCALAR: {
                float in = field_get(s, i, fn->i[0]), lo = fn->f[0] < fn->f[1] ? fn->f[0] : fn->f[1];
                float hi = fn->f[0] < fn->f[1] ? fn->f[1] : fn->f[0];
                if (fn->i[3] && (in < lo || in > hi)) break;
                field_set(s, i, fn->i[1], lerpf(fn->f[2], fn->f[3], saturatef(invlerpf(fn->f[0], fn->f[1], in))),
                          fn->i[2]);
                break;
            }
            case K_INIT_NOISE_SCALAR:
                field_set(s, i, fn->i[1], lerpf(fn->f[2], fn->f[3], rng_f(r)), 0);
                break;
            case K_INIT_REMAP_CP_SCALAR: {
                Vec3 cp = cp_get(s, fn->i[0]);
                float in = fn->i[4] == 1 ? cp.y : (fn->i[4] == 2 ? cp.z : cp.x);
                field_set(s, i, fn->i[1], lerpf(fn->f[2], fn->f[3], saturatef(invlerpf(fn->f[0], fn->f[1], in))),
                          fn->i[2]);
                break;
            }
            case K_INIT_PRE_AGE:
                s->spawn[i] -= lerpf(fn->f[0], fn->f[1], rng_f(r)) * s->life[i];
                break;
            default:
                break;
            }
        }
    }
    for (i = a; i < b; i++) {
        if (!(s->life[i] > 0.0f) || !finitef(s->life[i])) s->life[i] = 1e-3f;
        s->radius0[i] = s->radius[i];
        s->alpha0[i] = s->alpha[i];
        memcpy(&s->color0[3 * i], &s->color[3 * i], 3 * sizeof(float));
    }
}

/* ------------------------------------------------------------------ movement */

/* Force weights depend only on the system time, so they are worked out once per step
   instead of once per particle. Order is kept: K_FORCE_RANDOM draws from the scene rng. */
#define MAX_ACTIVE_FORCES 64

#if defined(__GNUC__)
#define PP_ALWAYS_INLINE static inline __attribute__((always_inline))
#else
#define PP_ALWAYS_INLINE static inline
#endif

typedef struct {
    const Func *fn;
    float w;
} ActiveForce;

/* returns the number of forces with a positive weight, or -1 when there are too many to list */
static int active_forces(const System *s, float w_time, ActiveForce *out) {
    const Def *d = s->def;
    int f, n = 0;
    for (f = 0; f < d->nfuncs; f++) {
        const Func *fn = &d->funcs[f];
        float w;
        if (fn->category != CAT_FORCE) continue;
        w = func_weight(fn, w_time);
        if (w <= 0.0f) continue;
        if (n == MAX_ACTIVE_FORCES) return -1;
        out[n].fn = fn;
        out[n].w = w;
        n++;
    }
    return n;
}

PP_ALWAYS_INLINE Vec3 force_term(Scene *sc, System *s, const Func *fn, float w, Vec3 pos, Vec3 sum) {
    switch (fn->kind) {
    case K_FORCE_RANDOM:
        sum = v3add(sum, v3scale(rand_box(&sc->rng, fn->v[0], fn->v[1]), w));
        break;
    case K_FORCE_TWIST: {
        Vec3 rel = v3sub(pos, cp_get(s, fn->i[0]));
        Vec3 t = v3norm(v3cross(fn->v[0], rel), v3(0, 0, 0));
        sum = v3add(sum, v3scale(t, fn->f[0] * w));
        break;
    }
    case K_FORCE_PULL: {
        Vec3 dvec = v3sub(cp_get(s, fn->i[0]), pos);
        float dist = v3len(dvec), fall;
        if (dist < 1e-3f) break;
        fall = dist > 1.0f ? powf(dist, fn->f[1]) : 1.0f;
        sum = v3add(sum, v3scale(dvec, fn->f[0] * w / (dist * fall)));
        break;
    }
    case K_FORCE_TURBULENT:
        sum = v3add(sum, v3scale(rand_box(&sc->rng, v3scale(fn->v[0], -1.0f), fn->v[0]), w));
        break;
    default:
        break;
    }
    return sum;
}

static Vec3 sum_active_forces(Scene *sc, System *s, int i, const ActiveForce *forces, int n) {
    Vec3 sum = v3(0, 0, 0), pos = v3(s->pos[3 * i], s->pos[3 * i + 1], s->pos[3 * i + 2]);
    int k;
    for (k = 0; k < n; k++) sum = force_term(sc, s, forces[k].fn, forces[k].w, pos, sum);
    return sum;
}

static Vec3 accumulate_forces(Scene *sc, System *s, int i, float w_time) {
    const Def *d = s->def;
    Vec3 sum = v3(0, 0, 0), pos = v3(s->pos[3 * i], s->pos[3 * i + 1], s->pos[3 * i + 2]);
    int f;
    for (f = 0; f < d->nfuncs; f++) {
        const Func *fn = &d->funcs[f];
        float w;
        if (fn->category != CAT_FORCE) continue;
        w = func_weight(fn, w_time);
        if (w <= 0.0f) continue;
        sum = force_term(sc, s, fn, w, pos, sum);
    }
    return sum;
}

static void apply_constraints(System *s, int i, int passes) {
    const Def *d = s->def;
    int f, pass;
    if (passes < 1) passes = 1;
    if (passes > 5) passes = 5;
    for (pass = 0; pass < passes; pass++) {
        for (f = 0; f < d->nfuncs; f++) {
            const Func *fn = &d->funcs[f];
            float *p = &s->pos[3 * i], *v = &s->vel[3 * i];
            if (fn->category != CAT_CONSTRAINT) continue;
            if (fn->kind == K_CON_DIST_TO_CP) {
                Vec3 c = v3add(cp_get(s, fn->i[0]), fn->v[0]), rel = v3sub(v3(p[0], p[1], p[2]), c);
                float l = v3len(rel), target = l;
                Vec3 dir = v3norm(rel, v3(0, 0, 1));
                if (l < fn->f[0]) target = fn->f[0];
                if (l > fn->f[1]) target = fn->f[1];
                if (target != l) {
                    p[0] = c.x + dir.x * target;
                    p[1] = c.y + dir.y * target;
                    p[2] = c.z + dir.z * target;
                }
            } else if (fn->kind == K_CON_PLANE) {
                Vec3 o = fn->i[1] ? fn->v[0] : v3add(cp_get(s, fn->i[0]), fn->v[0]), n = fn->v[1];
                float dist = v3dot(v3sub(v3(p[0], p[1], p[2]), o), n), vn = v[0] * n.x + v[1] * n.y + v[2] * n.z;
                if (dist < 0.0f) {
                    p[0] -= n.x * dist;
                    p[1] -= n.y * dist;
                    p[2] -= n.z * dist;
                    if (vn < 0.0f) {
                        v[0] -= n.x * vn;
                        v[1] -= n.y * vn;
                        v[2] -= n.z * vn;
                    }
                }
            }
        }
    }
}

/* ------------------------------------------------------------------ operators */

static void run_operators(Scene *sc, System *s, float dt) {
    const Def *d = s->def;
    float now = s->time, move_keep = 1.0f;
    int f, i, nforces = -1;
    ActiveForce forces[MAX_ACTIVE_FORCES];
    for (f = 0; f < d->nfuncs; f++) {
        const Func *fn = &d->funcs[f];
        float w;
        if (fn->category != CAT_OPERATOR) continue;
        w = func_weight(fn, now);
        if (fn->kind == K_OP_SET_CP_POSITIONS) {
            int n;
            for (n = 0; n < 4; n++) {
                int idx = fn->i[n];
                if (idx <= 0 || idx >= PP_MAX_CPS) continue;
                s->cp[idx] = fn->i[4] ? fn->v[n] : v3add(cp_get(s, fn->i[5]), fn->v[n]);
            }
            continue;
        }
        if (fn->kind == K_OP_MOVEMENT_BASIC) {
            /* per-step constants, identical for every particle */
            move_keep = fn->f[0] >= 1.0f ? 0.0f : powf(1.0f - fn->f[0], dt * 30.0f);
            nforces = active_forces(s, now, forces);
        }
        for (i = 0; i < s->count; i++) {
            float age = now - s->spawn[i], life = s->life[i], frac = age / life;
            float *p = &s->pos[3 * i], *v = &s->vel[3 * i];
            if (s->dead[i]) continue;
            switch (fn->kind) {
            case K_OP_MOVEMENT_BASIC: {
                Vec3 acc = v3add(fn->v[0], nforces >= 0 ? sum_active_forces(sc, s, i, forces, nforces)
                                                        : accumulate_forces(sc, s, i, now));
                float keep = move_keep;
                v[0] = (v[0] + acc.x * dt) * keep;
                v[1] = (v[1] + acc.y * dt) * keep;
                v[2] = (v[2] + acc.z * dt) * keep;
                p[0] += v[0] * dt;
                p[1] += v[1] * dt;
                p[2] += v[2] * dt;
                apply_constraints(s, i, fn->i[0]);
                break;
            }
            case K_OP_LIFESPAN_DECAY:
                if (age >= life) s->dead[i] = 1;
                break;
            case K_OP_ALPHA_FADE_DECAY: {
                float a = s->alpha0[i];
                if (age >= life) {
                    s->dead[i] = 1;
                    break;
                }
                if (frac <= fn->f[3]) a *= lerpf(fn->f[0], 1.0f, smoothstepf(invlerpf(fn->f[2], fn->f[3], frac)));
                if (frac >= fn->f[4]) a *= lerpf(1.0f, fn->f[1], smoothstepf(invlerpf(fn->f[4], fn->f[5], frac)));
                s->alpha[i] = a;
                break;
            }
            case K_OP_ALPHA_FADE_IN_RANDOM: {
                float end = range_exp(fn->f[0], fn->f[1], fn->f[2], hash_f(s->seed[i], fn->salt));
                float t = fn->i[0] ? frac : age;
                if (end > 0.0f && t < end) s->alpha[i] = s->alpha0[i] * smoothstepf(t / end);
                break;
            }
            case K_OP_ALPHA_FADE_OUT_RANDOM: {
                float ft = range_exp(fn->f[0], fn->f[1], fn->f[2], hash_f(s->seed[i], fn->salt));
                float t = fn->i[0] ? frac : age, end = fn->i[0] ? 1.0f : life, start = end - ft;
                if (t > start) s->alpha[i] = s->alpha0[i] * smoothstepf(invlerpf(end, start, t));
                break;
            }
            case K_OP_ALPHA_FADE_IN_SIMPLE:
                if (fn->f[0] > 0.0f && frac < fn->f[0]) s->alpha[i] = s->alpha0[i] * smoothstepf(frac / fn->f[0]);
                break;
            case K_OP_ALPHA_FADE_OUT_SIMPLE:
                if (fn->f[0] > 0.0f && frac > 1.0f - fn->f[0])
                    s->alpha[i] = s->alpha0[i] * smoothstepf((1.0f - frac) / fn->f[0]);
                break;
            case K_OP_RADIUS_SCALE: {
                float t = saturatef(invlerpf(fn->f[0], fn->f[1], frac));
                if (fn->i[0])
                    t = smoothstepf(t);
                else if (fn->f[4] != 0.5f)
                    t = schlick_bias(t, fn->f[4]);
                s->radius[i] = s->radius0[i] * lerpf(fn->f[2], fn->f[3], t);
                break;
            }
            case K_OP_COLOR_FADE: {
                float t = saturatef(invlerpf(fn->f[0], fn->f[1], frac));
                float *c = &s->color[3 * i], *c0 = &s->color0[3 * i];
                if (fn->i[0]) t = smoothstepf(t);
                c[0] = lerpf(c0[0], fn->v[0].x, t);
                c[1] = lerpf(c0[1], fn->v[0].y, t);
                c[2] = lerpf(c0[2], fn->v[0].z, t);
                break;
            }
            case K_OP_ROTATION_BASIC:
                s->rot[i] += s->rotspeed[i] * dt;
                break;
            case K_OP_SPIN_ROLL:
            case K_OP_SPIN_YAW: {
                float rate = fn->f[1] > 0.0f ? lerpf(fn->f[0], fn->f[2], saturatef(age / fn->f[1])) : fn->f[0];
                if (fn->kind == K_OP_SPIN_ROLL)
                    s->rot[i] += rate * dt * w;
                else
                    s->yaw[i] += rate * dt * w;
                break;
            }
            case K_OP_OSCILLATE_SCALAR:
            case K_OP_OSCILLATE_VECTOR: {
                float u0 = hash_f(s->seed[i], fn->salt), u1 = hash_f(s->seed[i], fn->salt + 1);
                float start = lerpf(fn->f[0], fn->f[1], hash_f(s->seed[i], fn->salt + 2));
                float end = lerpf(fn->f[2], fn->f[3], hash_f(s->seed[i], fn->salt + 3));
                float tw = fn->i[2] ? frac : age, to = fn->i[1] ? frac : age;
                if (tw < start || tw > end) break;
                if (fn->kind == K_OP_OSCILLATE_SCALAR) {
                    float *fp = field_ptr(s, i, fn->i[0]);
                    float rate = lerpf(fn->f[6], fn->f[7], u0), freq = lerpf(fn->f[8], fn->f[9], u1);
                    float delta = rate * pp_cos(PP_PI * (fn->f[4] * freq * to + fn->f[5])) * dt * w;
                    if (fp) *fp += rotation_field(fn->i[0]) ? delta * PP_DEG : delta;
                } else {
                    Vec3 rate = v3lerp(fn->v[0], fn->v[1], u0), freq = v3lerp(fn->v[2], fn->v[3], u1);
                    float *dst = fn->i[0] == F_TINT ? &s->color[3 * i] : (fn->i[0] == F_XYZ || fn->i[0] == F_PREV_XYZ ? p : NULL);
                    if (!dst) break;
                    dst[0] += rate.x * pp_cos(PP_PI * (fn->f[4] * freq.x * to + fn->f[5])) * dt * w;
                    dst[1] += rate.y * pp_cos(PP_PI * (fn->f[4] * freq.y * to + fn->f[5])) * dt * w;
                    dst[2] += rate.z * pp_cos(PP_PI * (fn->f[4] * freq.z * to + fn->f[5])) * dt * w;
                }
                break;
            }
            case K_OP_DAMPEN_TO_CP: {
                Vec3 cp = cp_get(s, fn->i[0]);
                float dist = v3len(v3sub(v3(p[0], p[1], p[2]), cp));
                if (fn->f[0] > 0.0f && dist < fn->f[0]) {
                    float keep = 1.0f - saturatef(fn->f[1]) * (1.0f - dist / fn->f[0]) * saturatef(dt * 30.0f);
                    v[0] *= keep;
                    v[1] *= keep;
                    v[2] *= keep;
                }
                break;
            }
            case K_OP_ROTATE_AROUND_AXIS: {
                Vec3 cp = cp_get(s, fn->i[0]);
                Vec3 rel = rotate_axis(v3sub(v3(p[0], p[1], p[2]), cp), fn->v[0], fn->f[0] * dt * w);
                Vec3 nv = rotate_axis(v3(v[0], v[1], v[2]), fn->v[0], fn->f[0] * dt * w);
                p[0] = cp.x + rel.x;
                p[1] = cp.y + rel.y;
                p[2] = cp.z + rel.z;
                v[0] = nv.x;
                v[1] = nv.y;
                v[2] = nv.z;
                break;
            }
            case K_OP_REMAP_DIST_TO_CP: {
                float dist = v3len(v3sub(v3(p[0], p[1], p[2]), cp_get(s, fn->i[0])));
                if (fn->i[3] && (dist < fn->f[0] || dist > fn->f[1])) break;
                field_set(s, i, fn->i[1], lerpf(fn->f[2], fn->f[3], saturatef(invlerpf(fn->f[0], fn->f[1], dist))),
                          fn->i[2]);
                break;
            }
            case K_OP_REMAP_SCALAR:
                field_set(s, i, fn->i[1],
                          lerpf(fn->f[2], fn->f[3], saturatef(invlerpf(fn->f[0], fn->f[1], field_get(s, i, fn->i[0])))), 0);
                break;
            case K_OP_REMAP_SPEED: {
                float speed = v3len(v3(v[0], v[1], v[2]));
                float out = lerpf(fn->f[2], fn->f[3], saturatef(invlerpf(fn->f[0], fn->f[1], speed)));
                if (fn->i[3]) {
                    float *field = field_ptr(s, i, fn->i[1]);
                    if (field && finitef(out)) *field *= out;
                } else {
                    field_set(s, i, fn->i[1], out, fn->i[2]);
                }
                break;
            }
            case K_OP_REMAP_CP_SCALAR: {
                Vec3 cp = cp_get(s, fn->i[0]);
                float in = fn->i[4] == 1 ? cp.y : (fn->i[4] == 2 ? cp.z : cp.x);
                field_set(s, i, fn->i[1], lerpf(fn->f[2], fn->f[3], saturatef(invlerpf(fn->f[0], fn->f[1], in))),
                          fn->i[2]);
                break;
            }
            default:
                break;
            }
        }
    }
}

static int particle_sane(const System *s, int i) {
    return finitef(s->pos[3 * i]) && finitef(s->pos[3 * i + 1]) && finitef(s->pos[3 * i + 2]) &&
           finitef(s->vel[3 * i]) && finitef(s->vel[3 * i + 1]) && finitef(s->vel[3 * i + 2]) &&
           finitef(s->radius[i]) && finitef(s->alpha[i]) && finitef(s->rot[i]) && finitef(s->yaw[i]) &&
           finitef(s->seq[i]) && finitef(s->trail[i]) && finitef(s->spawn[i]) && finitef(s->life[i]);
}

static void move_particle(System *s, int w, int r) {
    memcpy(&s->pos[3 * w], &s->pos[3 * r], 3 * sizeof(float));
    memcpy(&s->vel[3 * w], &s->vel[3 * r], 3 * sizeof(float));
    memcpy(&s->color[3 * w], &s->color[3 * r], 3 * sizeof(float));
    memcpy(&s->color0[3 * w], &s->color0[3 * r], 3 * sizeof(float));
    s->life[w] = s->life[r];
    s->spawn[w] = s->spawn[r];
    s->radius[w] = s->radius[r];
    s->radius0[w] = s->radius0[r];
    s->alpha[w] = s->alpha[r];
    s->alpha0[w] = s->alpha0[r];
    s->rot[w] = s->rot[r];
    s->rotspeed[w] = s->rotspeed[r];
    s->yaw[w] = s->yaw[r];
    s->seq[w] = s->seq[r];
    s->seq2[w] = s->seq2[r];
    s->trail[w] = s->trail[r];
    s->alpha2[w] = s->alpha2[r];
    s->seed[w] = s->seed[r];
    s->dead[w] = 0;
}

/* moves particles [r, r+n) down to [w, w+n), w < r */
static void move_particles(System *s, int w, int r, int n) {
    float *const vec3s[4] = {s->pos, s->vel, s->color, s->color0};
    float *const scalars[13] = {s->life, s->spawn, s->radius, s->radius0, s->alpha, s->alpha0, s->rot,
                                s->rotspeed, s->yaw, s->seq, s->seq2, s->trail, s->alpha2};
    int k;
    for (k = 0; k < 4; k++) memmove(&vec3s[k][3 * w], &vec3s[k][3 * r], (size_t)n * 3 * sizeof(float));
    for (k = 0; k < 13; k++) memmove(&scalars[k][w], &scalars[k][r], (size_t)n * sizeof(float));
    memmove(&s->seed[w], &s->seed[r], (size_t)n * sizeof(uint32_t));
    memset(&s->dead[w], 0, (size_t)n);
}

/* drops dead particles and any whose state became non-finite.
   Survivors move in runs: continuous emitters kill the oldest (first) particles,
   and moving one particle at a time shifted every array on every step. */
static void compact(System *s) {
    int r = 0, w = 0, n = s->count;
    while (r < n) {
        int start;
        if (s->dead[r] || !particle_sane(s, r)) {
            r++;
            continue;
        }
        start = r;
        while (r < n && !s->dead[r] && particle_sane(s, r)) r++;
        if (w != start) {
            if (r - start >= 16) {
                move_particles(s, w, start, r - start);
            } else {
                int k;
                for (k = 0; k < r - start; k++) move_particle(s, w + k, start + k);
            }
        }
        w += r - start;
    }
    s->count = w;
}

static void step_system(Scene *sc, System *s, float t_end) {
    float age0, age1;
    int first, c;
    memcpy(s->cp, s->parent ? s->parent->cp : sc->cp, sizeof(s->cp));
    if (t_end <= s->start) return;
    age1 = t_end - s->start;
    first = s->count;
    if (!s->started) {
        s->started = 1;
        age0 = 0.0f;
        spawn(sc, s, s->def->initial_particles, 0.0f, 0.0f);
    } else {
        age0 = s->time;
    }
    s->time = age1;
    run_emitters(sc, s, age0, age1);
    if (s->count > first) run_initializers(sc, s, first, s->count);
    run_operators(sc, s, age1 - age0 > 1e-6f ? age1 - age0 : 1e-6f);
    compact(s);
    for (c = 0; c < s->nchildren; c++) step_system(sc, s->children[c], t_end);
}

void pp_sim_step(Scene *sc, float dt) {
    if (!sc->systems || sc->nsystems == 0 || !sc->systems[0].def || !(dt > 0.0f) || !finitef(dt)) return;
    if (dt > 2.0f) dt = 2.0f;
    while (dt > 1e-6f) {
        float h = dt > (1.0f / 30.0f) ? (1.0f / 30.0f) : dt;
        step_system(sc, &sc->systems[0], sc->time + h);
        sc->time += h;
        dt -= h;
    }
}
