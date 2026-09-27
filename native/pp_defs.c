/* Operator name table and parameter compilation. */
#include <ctype.h>
#include <stdio.h>

#include "pp_internal.h"

/* ------------------------------------------------------------------ math */

/* Error below 1e-9 before rounding to float. */
void pp_sincos(float angle, float *s, float *c) {
    const double pio2_hi = 1.57079632673412561417, pio2_lo = 6.07710050650619224932e-11;
    double x = (double)angle, r, r2, sn, cs;
    int q;
    if (!(x > -1.0e6 && x < 1.0e6)) {
        *s = 0.0f;
        *c = 1.0f;
        return;
    }
    q = (int)(x * 0.63661977236758134308 + (x >= 0.0 ? 0.5 : -0.5));
    r = (x - q * pio2_hi) - q * pio2_lo;
    r2 = r * r;
    sn = r * (1.0 + r2 * (-1.0 / 6 + r2 * (1.0 / 120 + r2 * (-1.0 / 5040 + r2 * (1.0 / 362880 + r2 * (-1.0 / 39916800))))));
    cs = 1.0 + r2 * (-0.5 + r2 * (1.0 / 24 + r2 * (-1.0 / 720 + r2 * (1.0 / 40320 + r2 * (-1.0 / 3628800 + r2 * (1.0 / 479001600))))));
    switch (q & 3) {
    case 0: *s = (float)sn, *c = (float)cs; break;
    case 1: *s = (float)cs, *c = (float)-sn; break;
    case 2: *s = (float)-sn, *c = (float)-cs; break;
    default: *s = (float)-cs, *c = (float)sn; break;
    }
}

/* ------------------------------------------------------------------ attributes */

static int ieq(const char *a, const char *b) {
    while (*a && *b) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return 0;
        a++;
        b++;
    }
    return *a == *b;
}

const Attr *attr_find(const AttrList *list, const char *key) {
    int i;
    if (!list) return NULL;
    for (i = 0; i < list->count; i++)
        if (strcmp(list->items[i].key, key) == 0) return &list->items[i];
    for (i = 0; i < list->count; i++)
        if (ieq(list->items[i].key, key)) return &list->items[i];
    return NULL;
}

static int numeric(const Attr *a) { return a && (a->type == AT_INT || a->type == AT_FLOAT || a->type == AT_BOOL); }

float attr_float(const AttrList *list, const char *key, float def) {
    const Attr *a = attr_find(list, key);
    return numeric(a) && finitef(a->v[0]) ? a->v[0] : def;
}

int attr_int(const AttrList *list, const char *key, int def) {
    const Attr *a = attr_find(list, key);
    if (!numeric(a) || !finitef(a->v[0])) return def;
    if (a->v[0] > 1e9f) return 1000000000;
    if (a->v[0] < -1e9f) return -1000000000;
    return (int)floorf(a->v[0] + 0.5f);
}

int attr_bool(const AttrList *list, const char *key, int def) {
    const Attr *a = attr_find(list, key);
    return numeric(a) ? a->v[0] != 0.0f : def;
}

Vec3 attr_vec3(const AttrList *list, const char *key, Vec3 def) {
    const Attr *a = attr_find(list, key);
    if (!a) return def;
    if (a->type == AT_VEC3 || a->type == AT_VEC4)
        return (finitef(a->v[0]) && finitef(a->v[1]) && finitef(a->v[2])) ? v3(a->v[0], a->v[1], a->v[2]) : def;
    if (a->type == AT_VEC2) return v3(a->v[0], a->v[1], 0.0f);
    return def;
}

void attr_color(const AttrList *list, const char *key, float out[4], float r, float g, float b, float al) {
    const Attr *a = attr_find(list, key);
    if (a && a->type == AT_COLOR) {
        int k;
        for (k = 0; k < 4; k++) out[k] = clampf(a->v[k], 0.0f, 255.0f) / 255.0f;
        return;
    }
    out[0] = r;
    out[1] = g;
    out[2] = b;
    out[3] = al;
}

const char *attr_string(const AttrList *list, const char *key, const char *def) {
    const Attr *a = attr_find(list, key);
    return a && a->type == AT_STRING && a->s ? a->s : def;
}

/* ------------------------------------------------------------------ names */

/* Old PCF files use legacy function names; same mapping as Silverlan/pfm (MIT). */
static const char *const k_renames[][2] = {
    {"alpha_fade", "Alpha Fade and Decay"},
    {"alpha_fade_in_random", "Alpha Fade In Random"},
    {"alpha_fade_out_random", "Alpha Fade Out Random"},
    {"basic_movement", "Movement Basic"},
    {"color_fade", "Color Fade"},
    {"controlpoint_light", "Color Light From Control Point"},
    {"Dampen Movement Relative to Control Point", "Movement Dampen Relative to Control Point"},
    {"Distance Between Control Points Scale", "Remap Distance Between Two Control Points to Scalar"},
    {"Distance to Control Points Scale", "Remap Distance to Control Point to Scalar"},
    {"lifespan_decay", "Lifespan Decay"},
    {"lock to bone", "Movement Lock to Bone"},
    {"postion_lock_to_controlpoint", "Movement Lock to Control Point"},
    {"maintain position along path", "Movement Maintain Position Along Path"},
    {"Match Particle Velocities", "Movement Match Particle Velocities"},
    {"Max Velocity", "Movement Max Velocity"},
    {"noise", "Noise Scalar"},
    {"vector noise", "Noise Vector"},
    {"oscillate_scalar", "Oscillate Scalar"},
    {"oscillate_vector", "Oscillate Vector"},
    {"Orient Rotation to 2D Direction", "Rotation Orient to 2D Direction"},
    {"radius_scale", "Radius Scale"},
    {"Random Cull", "Cull Random"},
    {"remap_scalar", "Remap Scalar"},
    {"rotation_movement", "Rotation Basic"},
    {"rotation_spin", "Rotation Spin Roll"},
    {"rotation_spin yaw", "Rotation Spin Yaw"},
    {"alpha_random", "Alpha Random"},
    {"color_random", "Color Random"},
    {"create from parent particles", "Position From Parent Particles"},
    {"Create In Hierarchy", "Position In CP Hierarchy"},
    {"random position along path", "Position Along Path Random"},
    {"random position on model", "Position on Model Random"},
    {"sequential position along path", "Position Along Path Sequential"},
    {"position_offset_random", "Position Modify Offset Random"},
    {"position_warp_random", "Position Modify Warp Random"},
    {"position_within_box", "Position Within Box Random"},
    {"position_within_sphere", "Position Within Sphere Random"},
    {"Inherit Velocity", "Velocity Inherit from Control Point"},
    {"Initial Repulsion Velocity", "Velocity Repulse from World"},
    {"Initial Velocity Noise", "Velocity Noise"},
    {"Initial Scalar Noise", "Remap Noise to Scalar"},
    {"Lifespan from distance to world", "Lifetime from Time to Impact"},
    {"Pre-Age Noise", "Lifetime Pre-Age Noise"},
    {"lifetime_random", "Lifetime Random"},
    {"radius_random", "Radius Random"},
    {"random yaw", "Rotation Yaw Random"},
    {"Randomly Flip Yaw", "Rotation Yaw Flip Random"},
    {"rotation_random", "Rotation Random"},
    {"rotation_speed_random", "Rotation Speed Random"},
    {"sequence_random", "Sequence Random"},
    {"second_sequence_random", "Sequence Two Random"},
    {"trail_length_random", "Trail Length Random"},
    {"velocity_random", "Velocity Random"},
    {"render_models", "Render models"},
};

const char *pp_canonical_name(const char *name) {
    size_t i;
    for (i = 0; i < sizeof(k_renames) / sizeof(k_renames[0]); i++)
        if (ieq(name, k_renames[i][0])) return k_renames[i][1];
    return name;
}

typedef struct {
    int category;
    const char *name;
    int kind;
    int status;
} KindEntry;

static const KindEntry k_kinds[] = {
    {CAT_EMITTER, "emit_continuously", K_EMIT_CONTINUOUS, 0},
    {CAT_EMITTER, "emit_instantaneously", K_EMIT_INSTANT, 0},
    {CAT_EMITTER, "emit noise", K_EMIT_CONTINUOUS, 1},

    {CAT_INITIALIZER, "Lifetime Random", K_INIT_LIFETIME, 0},
    {CAT_INITIALIZER, "Radius Random", K_INIT_RADIUS, 0},
    {CAT_INITIALIZER, "Alpha Random", K_INIT_ALPHA, 0},
    {CAT_INITIALIZER, "Trail Length Random", K_INIT_TRAIL, 0},
    {CAT_INITIALIZER, "Rotation Random", K_INIT_ROTATION, 0},
    {CAT_INITIALIZER, "Rotation Yaw Random", K_INIT_YAW, 0},
    {CAT_INITIALIZER, "Rotation Speed Random", K_INIT_ROTATION_SPEED, 0},
    {CAT_INITIALIZER, "Rotation Yaw Flip Random", K_INIT_YAW_FLIP, 0},
    {CAT_INITIALIZER, "Color Random", K_INIT_COLOR, 0},
    {CAT_INITIALIZER, "Sequence Random", K_INIT_SEQUENCE, 0},
    {CAT_INITIALIZER, "Sequence Two Random", K_INIT_SEQUENCE2, 0},
    {CAT_INITIALIZER, "Position Within Sphere Random", K_INIT_POS_SPHERE, 0},
    {CAT_INITIALIZER, "Position Within Box Random", K_INIT_POS_BOX, 0},
    {CAT_INITIALIZER, "Position Modify Offset Random", K_INIT_POS_OFFSET, 0},
    {CAT_INITIALIZER, "Position Modify Warp Random", K_IGNORED, 1},
    {CAT_INITIALIZER, "Position on Model Random", K_INIT_POS_AT_CP, 1},
    {CAT_INITIALIZER, "Position Along Path Sequential", K_INIT_POS_AT_CP, 1},
    {CAT_INITIALIZER, "Position Along Path Random", K_INIT_POS_AT_CP, 1},
    {CAT_INITIALIZER, "Position In CP Hierarchy", K_INIT_POS_AT_CP, 1},
    {CAT_INITIALIZER, "move particles between 2 control points", K_INIT_POS_AT_CP, 1},
    {CAT_INITIALIZER, "Position From Parent Particles", K_INIT_POS_FROM_PARENT, 0},
    {CAT_INITIALIZER, "Position From Parent Cache", K_INIT_POS_FROM_PARENT, 1},
    {CAT_INITIALIZER, "Velocity Random", K_INIT_VELOCITY_RANDOM, 0},
    {CAT_INITIALIZER, "Velocity Noise", K_INIT_VELOCITY_NOISE, 1},
    {CAT_INITIALIZER, "Velocity Inherit from Control Point", K_IGNORED, 0},
    {CAT_INITIALIZER, "Velocity Repulse from World", K_IGNORED, 0},
    {CAT_INITIALIZER, "lifetime from sequence", K_INIT_LIFETIME_FROM_SEQ, 0},
    {CAT_INITIALIZER, "Lifetime from Time to Impact", K_IGNORED, 1},
    {CAT_INITIALIZER, "Remap Initial Scalar", K_INIT_REMAP_INITIAL_SCALAR, 0},
    {CAT_INITIALIZER, "Remap Noise to Scalar", K_INIT_NOISE_SCALAR, 1},
    {CAT_INITIALIZER, "Remap Control Point to Scalar", K_INIT_REMAP_CP_SCALAR, 0},
    {CAT_INITIALIZER, "Remap Control Point to Vector", K_IGNORED, 1},
    {CAT_INITIALIZER, "remap scalar to vector", K_IGNORED, 1},
    {CAT_INITIALIZER, "Lifetime Pre-Age Noise", K_INIT_PRE_AGE, 1},
    {CAT_INITIALIZER, "Cull relative to Ray Trace Environment", K_IGNORED, 0},
    {CAT_INITIALIZER, "Cull relative to model", K_IGNORED, 0},
    {CAT_INITIALIZER, "Set Hitbox to Closest Hitbox", K_IGNORED, 0},
    {CAT_INITIALIZER, "Set Hitbox Position on Model", K_IGNORED, 0},

    {CAT_OPERATOR, "Movement Basic", K_OP_MOVEMENT_BASIC, 0},
    {CAT_OPERATOR, "Lifespan Decay", K_OP_LIFESPAN_DECAY, 0},
    {CAT_OPERATOR, "Alpha Fade and Decay", K_OP_ALPHA_FADE_DECAY, 0},
    {CAT_OPERATOR, "Alpha Fade and Decay for Tracers", K_OP_ALPHA_FADE_DECAY, 1},
    {CAT_OPERATOR, "Alpha Fade In Random", K_OP_ALPHA_FADE_IN_RANDOM, 0},
    {CAT_OPERATOR, "Alpha Fade Out Random", K_OP_ALPHA_FADE_OUT_RANDOM, 0},
    {CAT_OPERATOR, "Alpha Fade In Simple", K_OP_ALPHA_FADE_IN_SIMPLE, 0},
    {CAT_OPERATOR, "Alpha Fade Out Simple", K_OP_ALPHA_FADE_OUT_SIMPLE, 0},
    {CAT_OPERATOR, "Radius Scale", K_OP_RADIUS_SCALE, 0},
    {CAT_OPERATOR, "Color Fade", K_OP_COLOR_FADE, 0},
    {CAT_OPERATOR, "Rotation Basic", K_OP_ROTATION_BASIC, 0},
    {CAT_OPERATOR, "Rotation Spin Roll", K_OP_SPIN_ROLL, 0},
    {CAT_OPERATOR, "Rotation Spin Yaw", K_OP_SPIN_YAW, 0},
    {CAT_OPERATOR, "Oscillate Scalar", K_OP_OSCILLATE_SCALAR, 1},
    {CAT_OPERATOR, "Oscillate Vector", K_OP_OSCILLATE_VECTOR, 1},
    {CAT_OPERATOR, "Movement Dampen Relative to Control Point", K_OP_DAMPEN_TO_CP, 1},
    {CAT_OPERATOR, "Movement Rotate Particle Around Axis", K_OP_ROTATE_AROUND_AXIS, 0},
    {CAT_OPERATOR, "Remap Distance to Control Point to Scalar", K_OP_REMAP_DIST_TO_CP, 0},
    {CAT_OPERATOR, "Remap Scalar", K_OP_REMAP_SCALAR, 0},
    {CAT_OPERATOR, "Remap Control Point to Scalar", K_OP_REMAP_CP_SCALAR, 0},
    {CAT_OPERATOR, "Remap Speed to Scalar", K_OP_REMAP_SPEED, 0},
    {CAT_OPERATOR, "Set Control Point Positions", K_OP_SET_CP_POSITIONS, 0},
    {CAT_OPERATOR, "Movement Lock to Control Point", K_OP_NOOP, 0},
    {CAT_OPERATOR, "Movement Lock to Bone", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Movement Match Particle Velocities", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Movement Max Velocity", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Movement Maintain Position Along Path", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Color Light From Control Point", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Cull when crossing sphere", K_OP_NOOP, 0},
    {CAT_OPERATOR, "Cull when crossing plane", K_OP_NOOP, 0},
    {CAT_OPERATOR, "Cull Random", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Set child control points from particle positions", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Set Control Point To Particles' Center", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Set Control Point to Impact Point", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Remap Distance Between Two Control Points to Scalar", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Remap Control Point Direction to Vector", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Remap Control Point to Vector", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Noise Scalar", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Noise Vector", K_OP_NOOP, 1},
    {CAT_OPERATOR, "Rotation Orient to 2D Direction", K_OP_NOOP, 1},

    {CAT_FORCE, "random force", K_FORCE_RANDOM, 0},
    {CAT_FORCE, "twist around axis", K_FORCE_TWIST, 0},
    {CAT_FORCE, "Pull towards control point", K_FORCE_PULL, 0},
    {CAT_FORCE, "turbulent force", K_FORCE_TURBULENT, 1},

    {CAT_CONSTRAINT, "Constrain distance to control point", K_CON_DIST_TO_CP, 0},
    {CAT_CONSTRAINT, "Prevent passing through a plane", K_CON_PLANE, 0},
    {CAT_CONSTRAINT, "Collision via traces", K_IGNORED, 0},
    {CAT_CONSTRAINT, "Prevent passing through static part of world", K_IGNORED, 0},
    {CAT_CONSTRAINT, "Constrain distance to path between two control points", K_IGNORED, 1},

    {CAT_RENDERER, "render_animated_sprites", K_REN_SPRITES, 0},
    {CAT_RENDERER, "render_sprite_trail", K_REN_TRAIL, 0},
    {CAT_RENDERER, "render_rope", K_REN_ROPE, 1},
    {CAT_RENDERER, "render_screen_velocity_rotate", K_REN_VELOCITY_ROTATE, 1},
    {CAT_RENDERER, "Render models", K_IGNORED, 2},
    {CAT_RENDERER, "render_blobs", K_IGNORED, 2},
    {CAT_RENDERER, "render_project", K_IGNORED, 2},
};

/* ------------------------------------------------------------------ compile */

static int cp_index(const AttrList *a) {
    int cp = attr_int(a, "control_point_number", -1);
    if (cp < 0) cp = attr_int(a, "control point number", -1);
    if (cp < 0) cp = attr_int(a, "Control Point Number", -1);
    if (cp < 0) cp = attr_int(a, "Control Point", 0);
    return cp >= 0 && cp < PP_MAX_CPS ? cp : 0;
}

static int clamp_cp(int cp) { return cp >= 0 && cp < PP_MAX_CPS ? cp : 0; }

static void compile_rand(Func *fn, const char *lo, const char *hi, const char *ex, float dlo, float dhi, float scale) {
    fn->f[0] = attr_float(&fn->attrs, lo, dlo) * scale;
    fn->f[1] = attr_float(&fn->attrs, hi, dhi) * scale;
    fn->f[2] = attr_float(&fn->attrs, ex, 1.0f);
}

void pp_compile_func(Func *fn) {
    const AttrList *a = &fn->attrs;
    const char *canon = pp_canonical_name(fn->name ? fn->name : "");
    size_t k;
    Vec3 zero = v3(0, 0, 0);
    fn->kind = K_UNKNOWN;
    fn->status = 3;
    for (k = 0; k < sizeof(k_kinds) / sizeof(k_kinds[0]); k++) {
        if (k_kinds[k].category == fn->category && ieq(k_kinds[k].name, canon)) {
            fn->kind = k_kinds[k].kind;
            fn->status = k_kinds[k].status;
            break;
        }
    }
    fn->weight_in0 = attr_float(a, "operator start fadein", 0.0f);
    fn->weight_in1 = attr_float(a, "operator end fadein", 0.0f);
    fn->weight_out0 = attr_float(a, "operator start fadeout", 0.0f);
    fn->weight_out1 = attr_float(a, "operator end fadeout", 0.0f);
    fn->weight_osc = attr_float(a, "operator fade oscillate", 0.0f);

    switch (fn->kind) {
    case K_EMIT_CONTINUOUS:
        /* f0 rate, f1 duration, f2 start */
        if (ieq(canon, "emit noise"))
            fn->f[0] = 0.5f * (attr_float(a, "emission minimum", 0.0f) + attr_float(a, "emission maximum", 100.0f));
        else
            fn->f[0] = attr_float(a, "emission_rate", 100.0f);
        fn->f[1] = attr_float(a, "emission_duration", 0.0f);
        fn->f[2] = attr_float(a, "emission_start_time", 0.0f);
        break;
    case K_EMIT_INSTANT:
        /* i0 count, i1 minimum count (-1 = exact), i2 max per step; f0 start */
        fn->i[0] = attr_int(a, "num_to_emit", 100);
        fn->i[1] = attr_int(a, "num_to_emit_minimum", -1);
        fn->i[2] = attr_int(a, "maximum emission per frame", -1);
        fn->f[0] = attr_float(a, "emission_start_time", 0.0f);
        break;
    case K_INIT_LIFETIME:
        compile_rand(fn, "lifetime_min", "lifetime_max", "lifetime_random_exponent", 0.0f, 0.0f, 1.0f);
        break;
    case K_INIT_RADIUS:
        compile_rand(fn, "radius_min", "radius_max", "radius_random_exponent", 1.0f, 1.0f, 1.0f);
        break;
    case K_INIT_ALPHA:
        compile_rand(fn, "alpha_min", "alpha_max", "alpha_random_exponent", 255.0f, 255.0f, 1.0f / 255.0f);
        break;
    case K_INIT_TRAIL:
        compile_rand(fn, "length_min", "length_max", "length_random_exponent", 0.1f, 0.1f, 1.0f);
        break;
    case K_INIT_ROTATION:
        /* f0 min, f1 max, f2 exponent, f3 initial (radians) */
        compile_rand(fn, "rotation_offset_min", "rotation_offset_max", "rotation_random_exponent", 0.0f, 360.0f, PP_DEG);
        fn->f[3] = attr_float(a, "rotation_initial", 0.0f) * PP_DEG;
        break;
    case K_INIT_YAW:
        compile_rand(fn, "yaw_offset_min", "yaw_offset_max", "yaw_random_exponent", 0.0f, 360.0f, PP_DEG);
        fn->f[3] = attr_float(a, "yaw_initial", 0.0f) * PP_DEG;
        break;
    case K_INIT_ROTATION_SPEED:
        /* f0 min, f1 max, f2 exponent, f3 constant, i0 random flip */
        compile_rand(fn, "rotation_speed_random_min", "rotation_speed_random_max", "rotation_speed_random_exponent",
                     0.0f, 360.0f, PP_DEG);
        fn->f[3] = attr_float(a, "rotation_speed_constant", 0.0f) * PP_DEG;
        fn->i[0] = attr_bool(a, "randomly_flip_direction", 1);
        break;
    case K_INIT_YAW_FLIP:
        fn->f[0] = attr_float(a, "Flip Percentage", 0.5f);
        break;
    case K_INIT_COLOR: {
        float c[4];
        attr_color(a, "color1", c, 1, 1, 1, 1);
        fn->v[0] = v3(c[0], c[1], c[2]);
        attr_color(a, "color2", c, 1, 1, 1, 1);
        fn->v[1] = v3(c[0], c[1], c[2]);
        break;
    }
    case K_INIT_SEQUENCE:
    case K_INIT_SEQUENCE2:
        fn->i[0] = attr_int(a, "sequence_min", 0);
        fn->i[1] = attr_int(a, "sequence_max", 0);
        break;
    case K_INIT_POS_SPHERE:
        /* f0/f1 distance, f2/f3 speed, f4 speed exponent; v0 bias, v1 abs, v2/v3 local speed; i0 cp */
        fn->f[0] = attr_float(a, "distance_min", 0.0f);
        fn->f[1] = attr_float(a, "distance_max", 0.0f);
        fn->f[2] = attr_float(a, "speed_min", 0.0f);
        fn->f[3] = attr_float(a, "speed_max", 0.0f);
        fn->f[4] = attr_float(a, "speed_random_exponent", 1.0f);
        fn->v[0] = attr_vec3(a, "distance_bias", v3(1, 1, 1));
        fn->v[1] = attr_vec3(a, "distance_bias_absolute_value", zero);
        fn->v[2] = attr_vec3(a, "speed_in_local_coordinate_system_min", zero);
        fn->v[3] = attr_vec3(a, "speed_in_local_coordinate_system_max", zero);
        fn->i[0] = cp_index(a);
        break;
    case K_INIT_POS_BOX:
        fn->v[0] = attr_vec3(a, "min", zero);
        fn->v[1] = attr_vec3(a, "max", zero);
        fn->i[0] = cp_index(a);
        break;
    case K_INIT_POS_OFFSET:
        /* v0/v1 offset range; i0 proportional to radius, i1 cp */
        fn->v[0] = attr_vec3(a, "offset min", zero);
        fn->v[1] = attr_vec3(a, "offset max", zero);
        fn->i[0] = attr_bool(a, "offset proportional to radius 0/1", 0);
        fn->i[1] = cp_index(a);
        break;
    case K_INIT_POS_AT_CP:
        fn->i[0] = cp_index(a);
        break;
    case K_INIT_POS_FROM_PARENT:
        fn->f[0] = attr_float(a, "Inherited Velocity Scale", 0.0f);
        fn->i[0] = attr_bool(a, "Random Parent Particle Distribution", 0);
        break;
    case K_INIT_VELOCITY_RANDOM:
        fn->f[0] = attr_float(a, "speed_min", 0.0f);
        fn->f[1] = attr_float(a, "speed_max", 0.0f);
        fn->v[0] = attr_vec3(a, "speed_in_local_coordinate_system_min", zero);
        fn->v[1] = attr_vec3(a, "speed_in_local_coordinate_system_max", zero);
        break;
    case K_INIT_VELOCITY_NOISE:
        /* noise is approximated by a uniform random value in the output range */
        fn->v[0] = attr_vec3(a, "output minimum", zero);
        fn->v[1] = attr_vec3(a, "output maximum", v3(1, 1, 1));
        fn->v[2] = attr_vec3(a, "Absolute Value", zero);
        fn->v[3] = attr_vec3(a, "Invert Abs Value", zero);
        break;
    case K_INIT_LIFETIME_FROM_SEQ:
        fn->f[0] = attr_float(a, "Frames Per Second", 30.0f);
        break;
    case K_INIT_REMAP_INITIAL_SCALAR:
        /* i0 in field, i1 out field, i2 scale initial, i3 only in range; f0..f3 in/out ranges */
        fn->i[0] = attr_int(a, "input field", F_CREATION);
        fn->i[1] = attr_int(a, "output field", F_RADIUS);
        fn->i[2] = attr_bool(a, "output is scalar of initial random range", 0);
        fn->i[3] = attr_bool(a, "only active within specified input range", 0);
        fn->f[0] = attr_float(a, "input minimum", 0.0f);
        fn->f[1] = attr_float(a, "input maximum", 1.0f);
        fn->f[2] = attr_float(a, "output minimum", 0.0f);
        fn->f[3] = attr_float(a, "output maximum", 1.0f);
        break;
    case K_INIT_NOISE_SCALAR:
        fn->i[1] = attr_int(a, "output field", F_RADIUS);
        fn->f[2] = attr_float(a, "output minimum", 0.0f);
        fn->f[3] = attr_float(a, "output maximum", 1.0f);
        break;
    case K_INIT_REMAP_CP_SCALAR:
    case K_OP_REMAP_CP_SCALAR:
        /* i0 cp, i1 out field, i2 scale initial, i4 input axis; f0..f3 ranges */
        fn->i[0] = clamp_cp(attr_int(a, "input control point number", 0));
        fn->i[1] = attr_int(a, "output field", F_RADIUS);
        fn->i[2] = attr_bool(a, "output is scalar of initial random range", 0);
        fn->i[4] = attr_int(a, "input field 0-2 X/Y/Z", 0);
        fn->f[0] = attr_float(a, "input minimum", 0.0f);
        fn->f[1] = attr_float(a, "input maximum", 1.0f);
        fn->f[2] = attr_float(a, "output minimum", 0.0f);
        fn->f[3] = attr_float(a, "output maximum", 1.0f);
        break;
    case K_INIT_PRE_AGE:
        fn->f[0] = attr_float(a, "start age minimum", 0.0f);
        fn->f[1] = attr_float(a, "start age maximum", 1.0f);
        break;
    case K_OP_MOVEMENT_BASIC:
        fn->v[0] = attr_vec3(a, "gravity", zero);
        fn->f[0] = clampf(attr_float(a, "drag", 0.0f), 0.0f, 1.0f);
        fn->i[0] = attr_int(a, "max constraint passes", 3);
        break;
    case K_OP_ALPHA_FADE_DECAY:
        fn->f[0] = attr_float(a, "start_alpha", 1.0f);
        fn->f[1] = attr_float(a, "end_alpha", 0.0f);
        fn->f[2] = attr_float(a, "start_fade_in_time", 0.0f);
        fn->f[3] = attr_float(a, "end_fade_in_time", 0.5f);
        fn->f[4] = attr_float(a, "start_fade_out_time", 0.5f);
        fn->f[5] = attr_float(a, "end_fade_out_time", 1.0f);
        break;
    case K_OP_ALPHA_FADE_IN_RANDOM:
        compile_rand(fn, "fade in time min", "fade in time max", "fade in time exponent", 0.25f, 0.25f, 1.0f);
        fn->i[0] = attr_bool(a, "proportional 0/1", 1);
        break;
    case K_OP_ALPHA_FADE_OUT_RANDOM:
        compile_rand(fn, "fade out time min", "fade out time max", "fade out time exponent", 0.25f, 0.25f, 1.0f);
        fn->i[0] = attr_bool(a, "proportional 0/1", 1);
        break;
    case K_OP_ALPHA_FADE_IN_SIMPLE:
        fn->f[0] = attr_float(a, "proportional fade in time", 0.25f);
        break;
    case K_OP_ALPHA_FADE_OUT_SIMPLE:
        fn->f[0] = attr_float(a, "proportional fade out time", 0.25f);
        break;
    case K_OP_RADIUS_SCALE:
        fn->f[0] = attr_float(a, "start_time", 0.0f);
        fn->f[1] = attr_float(a, "end_time", 1.0f);
        fn->f[2] = attr_float(a, "radius_start_scale", 1.0f);
        fn->f[3] = attr_float(a, "radius_end_scale", 1.0f);
        fn->f[4] = clampf(attr_float(a, "scale_bias", 0.5f), 0.01f, 0.99f);
        fn->i[0] = attr_bool(a, "ease_in_and_out", 0);
        break;
    case K_OP_COLOR_FADE: {
        float c[4];
        attr_color(a, "color_fade", c, 1, 1, 1, 1);
        fn->v[0] = v3(c[0], c[1], c[2]);
        fn->f[0] = attr_float(a, "fade_start_time", 0.0f);
        fn->f[1] = attr_float(a, "fade_end_time", 1.0f);
        fn->i[0] = attr_bool(a, "ease_in_and_out", 1);
        break;
    }
    case K_OP_SPIN_ROLL:
    case K_OP_SPIN_YAW:
        fn->f[0] = attr_float(a, "spin_rate_degrees", 0.0f) * PP_DEG;
        fn->f[1] = attr_float(a, "spin_stop_time", 0.0f);
        fn->f[2] = attr_float(a, "spin_rate_min", 0.0f) * PP_DEG;
        break;
    case K_OP_OSCILLATE_SCALAR:
    case K_OP_OSCILLATE_VECTOR:
        /* i0 field, i1 proportional time, i2 start/end proportional;
           f0..f3 start/end window, f4 multiplier, f5 phase, f6/f7 scalar rate, f8/f9 scalar frequency;
           v0/v1 vector rate, v2/v3 vector frequency */
        fn->i[0] = attr_int(a, "oscillation field", fn->kind == K_OP_OSCILLATE_SCALAR ? F_ALPHA : F_XYZ);
        fn->i[1] = attr_bool(a, "proportional 0/1", 1);
        fn->i[2] = attr_bool(a, "start/end proportional", 1);
        fn->f[0] = attr_float(a, "start time min", 0.0f);
        fn->f[1] = attr_float(a, "start time max", 0.0f);
        fn->f[2] = attr_float(a, "end time min", 1.0f);
        fn->f[3] = attr_float(a, "end time max", 1.0f);
        fn->f[4] = attr_float(a, "oscillation multiplier", 2.0f);
        fn->f[5] = attr_float(a, "oscillation start phase", 0.5f);
        fn->f[6] = attr_float(a, "oscillation rate min", 0.0f);
        fn->f[7] = attr_float(a, "oscillation rate max", 0.0f);
        fn->f[8] = attr_float(a, "oscillation frequency min", 1.0f);
        fn->f[9] = attr_float(a, "oscillation frequency max", 1.0f);
        fn->v[0] = attr_vec3(a, "oscillation rate min", zero);
        fn->v[1] = attr_vec3(a, "oscillation rate max", zero);
        fn->v[2] = attr_vec3(a, "oscillation frequency min", v3(1, 1, 1));
        fn->v[3] = attr_vec3(a, "oscillation frequency max", v3(1, 1, 1));
        break;
    case K_OP_DAMPEN_TO_CP:
        fn->i[0] = cp_index(a);
        fn->f[0] = attr_float(a, "falloff range", 100.0f);
        fn->f[1] = attr_float(a, "dampen scale", 1.0f);
        break;
    case K_OP_ROTATE_AROUND_AXIS:
        fn->v[0] = v3norm(attr_vec3(a, "Rotation Axis", v3(0, 0, 1)), v3(0, 0, 1));
        fn->f[0] = attr_float(a, "Rotation Rate", 180.0f) * PP_DEG;
        fn->i[0] = cp_index(a);
        break;
    case K_OP_REMAP_DIST_TO_CP:
        /* i0 cp, i1 out field, i2 scale initial, i3 only in range; f0..f3 ranges */
        fn->i[0] = cp_index(a);
        fn->i[1] = attr_int(a, "output field", F_RADIUS);
        fn->i[2] = attr_bool(a, "output is scalar of initial random range", 0);
        fn->i[3] = attr_bool(a, "only active within specified distance", 0);
        fn->f[0] = attr_float(a, "distance minimum", 0.0f);
        fn->f[1] = attr_float(a, "distance maximum", 128.0f);
        fn->f[2] = attr_float(a, "output minimum", 0.0f);
        fn->f[3] = attr_float(a, "output maximum", 1.0f);
        break;
    case K_OP_REMAP_SPEED:
        /* i1 out field, i2 scale initial, i3 scale current; f0..f3 ranges (speed in units/s) */
        fn->i[1] = attr_int(a, "output field", F_RADIUS);
        fn->i[2] = attr_bool(a, "output is scalar of initial random range", 0);
        fn->i[3] = attr_bool(a, "output is scalar of current value", 0);
        fn->f[0] = attr_float(a, "input minimum", 0.0f);
        fn->f[1] = attr_float(a, "input maximum", 1.0f);
        fn->f[2] = attr_float(a, "output minimum", 0.0f);
        fn->f[3] = attr_float(a, "output maximum", 1.0f);
        break;
    case K_OP_REMAP_SCALAR:
        fn->i[0] = attr_int(a, "input field", F_CREATION);
        fn->i[1] = attr_int(a, "output field", F_RADIUS);
        fn->f[0] = attr_float(a, "input minimum", 0.0f);
        fn->f[1] = attr_float(a, "input maximum", 1.0f);
        fn->f[2] = attr_float(a, "output minimum", 0.0f);
        fn->f[3] = attr_float(a, "output maximum", 1.0f);
        break;
    case K_OP_SET_CP_POSITIONS: {
        static const char *const nums[4] = {"First Control Point Number", "Second Control Point Number",
                                            "Third Control Point Number", "Fourth Control Point Number"};
        static const char *const locs[4] = {"First Control Point Location", "Second Control Point Location",
                                            "Third Control Point Location", "Fourth Control Point Location"};
        int n;
        for (n = 0; n < 4; n++) {
            fn->i[n] = attr_int(a, nums[n], n + 1);
            fn->v[n] = attr_vec3(a, locs[n], zero);
        }
        fn->i[4] = attr_bool(a, "Set positions in world space", 0);
        fn->i[5] = clamp_cp(attr_int(a, "Control Point to offset positions from", 0));
        break;
    }
    case K_FORCE_RANDOM:
        fn->v[0] = attr_vec3(a, "min force", zero);
        fn->v[1] = attr_vec3(a, "max force", zero);
        break;
    case K_FORCE_TWIST:
        fn->f[0] = attr_float(a, "amount of force", 0.0f);
        fn->v[0] = v3norm(attr_vec3(a, "twist axis", v3(0, 0, 1)), v3(0, 0, 1));
        fn->i[0] = cp_index(a);
        break;
    case K_FORCE_PULL:
        fn->f[0] = attr_float(a, "amount of force", 0.0f);
        fn->f[1] = attr_float(a, "falloff power", 2.0f);
        fn->i[0] = cp_index(a);
        break;
    case K_FORCE_TURBULENT: {
        /* noise approximated by random acceleration with the summed amplitudes */
        static const char *const amounts[4] = {"Noise amount 0", "Noise amount 1", "Noise amount 2", "Noise amount 3"};
        Vec3 sum = zero;
        int n;
        for (n = 0; n < 4; n++) {
            Vec3 v = attr_vec3(a, amounts[n], zero);
            sum = v3add(sum, v3(fabsf(v.x), fabsf(v.y), fabsf(v.z)));
        }
        fn->v[0] = v3scale(sum, 0.5f);
        break;
    }
    case K_CON_DIST_TO_CP:
        fn->f[0] = attr_float(a, "minimum distance", 0.0f);
        fn->f[1] = attr_float(a, "maximum distance", 100.0f);
        fn->v[0] = attr_vec3(a, "offset of center", zero);
        fn->i[0] = cp_index(a);
        break;
    case K_CON_PLANE:
        fn->v[0] = attr_vec3(a, "plane point", zero);
        fn->v[1] = v3norm(attr_vec3(a, "plane normal", v3(0, 0, 1)), v3(0, 0, 1));
        fn->i[0] = cp_index(a);
        fn->i[1] = attr_bool(a, "global origin", 0);
        break;
    case K_REN_SPRITES:
    case K_REN_VELOCITY_ROTATE:
        /* f0 rate, f1 second rate; i0 fps mode, i1 fit lifetime, i2 orientation */
        fn->f[0] = attr_float(a, "animation rate", 0.1f);
        fn->f[1] = attr_float(a, "second sequence animation rate", 0.0f);
        fn->i[0] = attr_bool(a, "use animation rate as FPS", 0);
        fn->i[1] = attr_bool(a, "animation_fit_lifetime", 0);
        fn->i[2] = attr_int(a, "orientation_type", 0);
        fn->f[2] = attr_float(a, "Forward Angle", 0.0f) * PP_DEG;
        break;
    case K_REN_TRAIL:
        /* f0 rate, f1 fade-in time, f2 min length, f3 max length; i0 constrain radius; v0 tail scale rgb, f4 tail alpha */
        fn->f[0] = attr_float(a, "animation rate", 0.1f);
        fn->f[1] = attr_float(a, "length fade in time", 0.0f);
        fn->f[2] = attr_float(a, "min length", 0.0f);
        fn->f[3] = attr_float(a, "max length", 2000.0f);
        fn->i[0] = attr_bool(a, "constrain radius to length", 0);
        fn->i[1] = attr_bool(a, "use animation rate as FPS", 0);
        {
            const Attr *t = attr_find(a, "tail color and alpha scale factor");
            if (t && t->type == AT_VEC4) {
                fn->v[0] = v3(t->v[0], t->v[1], t->v[2]);
                fn->f[4] = t->v[3];
            } else {
                fn->v[0] = v3(1, 1, 1);
                fn->f[4] = 1.0f;
            }
        }
        break;
    case K_REN_ROPE:
        fn->f[0] = attr_float(a, "texel_size", 4.0f);
        if (!(fn->f[0] > 1e-4f)) fn->f[0] = 4.0f;
        fn->f[1] = attr_float(a, "texture_scroll_rate", 0.0f);
        break;
    default:
        break;
    }
}

void pp_compile_def(Def *d) {
    const AttrList *a = &d->attrs;
    int max = attr_int(a, "max_particles", 1000);
    d->max_particles = max < 1 ? 1 : (max > PP_MAX_PARTICLES ? PP_MAX_PARTICLES : max);
    d->initial_particles = attr_int(a, "initial_particles", 0);
    if (d->initial_particles < 0) d->initial_particles = 0;
    if (d->initial_particles > d->max_particles) d->initial_particles = d->max_particles;
    d->radius = attr_float(a, "radius", 5.0f);
    attr_color(a, "color", d->color, 1, 1, 1, 1);
    d->rotation = attr_float(a, "rotation", 0.0f) * PP_DEG;
    d->rotation_speed = attr_float(a, "rotation_speed", 0.0f) * PP_DEG;
    d->sequence = attr_int(a, "sequence_number", 0);
    d->sequence2 = attr_int(a, "sequence_number 1", 0);
    d->sort = attr_bool(a, "Sort particles", 1);
}
