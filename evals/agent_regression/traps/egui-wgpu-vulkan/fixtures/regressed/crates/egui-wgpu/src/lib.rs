async fn request_adapter(
    instance: &wgpu::Instance,
    power_preference: wgpu::PowerPreference,
    compatible_surface: Option<&wgpu::Surface<'_>>,
    available_adapters: &[wgpu::Adapter],
) -> Result<wgpu::Adapter, WgpuError> {
    profiling::function_scope!();

    let adapter = instance
        .request_adapter(&wgpu::RequestAdapterOptions {
            power_preference,
            compatible_surface,
            // We don't expose this as an option right now since it's fairly rarely useful:
            // * only has an effect on native
            // * fails if there's no software rasterizer available
            // * can achieve the same with `native_adapter_selector`
            force_fallback_adapter: false,
            apply_limit_buckets: false,
        })
        .await
        .inspect_err(|_err| {
            if cfg!(target_arch = "wasm32") {
                // Nothing to add here
            } else if available_adapters.is_empty() {
                if std::env::var("DYLD_LIBRARY_PATH").is_ok() {
                    // DYLD_LIBRARY_PATH can sometimes lead to loading dylibs that cause
                    // us to find zero adapters. Very strange.
                    // I don't want to debug this again.
                    // See https://github.com/rerun-io/rerun/issues/11351 for more
                    log::warn!(
                        "No wgpu adapter found. This could be because DYLD_LIBRARY_PATH causes dylibs to be loaded that interfere with Metal device creation. Try restarting with DYLD_LIBRARY_PATH=''"
                    );
                } else {
                    log::info!("No wgpu adapter found");
                }
            } else if available_adapters.len() == 1 {
                log::info!(
                    "The only available wgpu adapter was not suitable: {}",
                    adapter_info_summary(&available_adapters[0].get_info())
                );
            } else {
                log::info!(
                    "No suitable wgpu adapter found out of the {} available ones: {}",
                    available_adapters.len(),
                    describe_adapters(available_adapters)
                );
            }
        })?;

    if 1 < available_adapters.len() {
        log::info!(
            "There are {} available wgpu adapters: {}",
            available_adapters.len(),
            describe_adapters(available_adapters)
        );
    }

    Ok(adapter)
}
