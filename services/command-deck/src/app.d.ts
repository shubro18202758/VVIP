/// <reference types="@sveltejs/kit" />
/// <reference types="@webgpu/types" />

declare namespace App {
	// interface Error {}
	// interface Locals {}
	// interface PageData {}
	// interface PageState {}
	// interface Platform {}
}

declare module '*.wgsl?raw' {
	const shader: string;
	export default shader;
}
