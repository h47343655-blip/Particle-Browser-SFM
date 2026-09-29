/*
 * pp_render: a small self-contained particle preview renderer for SFM's
 * Particle Browser.  It simulates the common subset of Source particle
 * operators and software-renders them, so the picker can show an isolated
 * preview with its own camera without touching SFM's engine.
 *
 * All functions use the C calling convention and are meant for ctypes.
 * Handles are small positive integers; 0 means failure (see pp_last_error).
 */
#ifndef PP_RENDER_H
#define PP_RENDER_H

#ifdef _WIN32
#define PP_API __declspec(dllexport)
#else
#define PP_API __attribute__((visibility("default")))
#endif

#define PP_ABI_VERSION 1

/* material flags */
#define PP_MAT_ADDITIVE 1
#define PP_MAT_NO_BLEND_FRAMES 2
#define PP_MAT_INVISIBLE 4 /* e.g. refraction: simulated (bounds, children) but never drawn */

/* render options */
#define PP_OPT_GRID 1
#define PP_OPT_AXES 2
#define PP_OPT_EDITOR_GRID 4 /* grid as in the Particle Editor: bright, even, two pixels wide (older DLLs ignore it) */

#ifdef __cplusplus
extern "C" {
#endif

PP_API int pp_abi_version(void);
PP_API const char *pp_last_error(void);
PP_API int pp_self_test(void);

PP_API int pp_material_create_vtf(const unsigned char *data, int size, int flags, float overbright);
PP_API int pp_material_create_rgba(const unsigned char *rgba, int width, int height, int flags,
                                   float overbright);
PP_API void pp_material_destroy(int material);
PP_API int pp_material_info(int material, int *width, int *height, int *sequences, int *mips);
PP_API int pp_material_live_count(void);

PP_API int pp_scene_create(void);
PP_API void pp_scene_destroy(int scene);
PP_API int pp_scene_load(int scene, const unsigned char *blob, int size, const char *root);
PP_API int pp_scene_material_count(int scene);
PP_API const char *pp_scene_material_name(int scene, int index);
PP_API int pp_scene_bind_material(int scene, const char *name, int material);
PP_API int pp_scene_issue_count(int scene);
PP_API const char *pp_scene_issue(int scene, int index);
PP_API void pp_scene_restart(int scene, unsigned int seed);
PP_API void pp_scene_step(int scene, float seconds);
PP_API float pp_scene_time(int scene);
PP_API int pp_scene_particle_count(int scene);
PP_API int pp_scene_system_count(int scene);
PP_API int pp_scene_bounds(int scene, float *out6);
PP_API void pp_scene_set_camera(int scene, float yaw_deg, float pitch_deg, float distance,
                                float target_x, float target_y, float target_z, float fov_deg);
PP_API void pp_scene_set_options(int scene, int flags, unsigned int top_rgb, unsigned int bottom_rgb);
PP_API void pp_scene_set_control_point(int scene, int index, float x, float y, float z);
PP_API int pp_scene_render(int scene, unsigned char *out_bgra, int width, int height, int stride);

/* Optional (added without an ABI bump; callers check that the export exists).
   Rendering threads: 0 = automatic (logical cores - 1, at most 8), 1 = single-threaded.
   Returns the count now in use.  The frame is bit-identical for every thread count. */
PP_API int pp_set_render_threads(int count);
PP_API int pp_render_thread_count(void);
/* Optional as well: particles the scene can hold at once (its systems' max_particles, as capped). */
PP_API int pp_scene_particle_capacity(int scene);

#ifdef __cplusplus
}
#endif

#endif
