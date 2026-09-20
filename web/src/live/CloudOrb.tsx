import { useEffect, useRef } from 'react'
import type { AgentState } from './agentSession'

const VERTEX = 'attribute vec2 position; varying vec2 uv; void main(){ uv=position*.5+.5; gl_Position=vec4(position,0.,1.); }'
const FRAGMENT = `
precision highp float;
varying vec2 uv;
uniform float time;
uniform float energy;
float hash(vec3 p){p=fract(p*.3183099+vec3(.1,.2,.3)); p*=17.; return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}
float noise(vec3 x){
  vec3 i=floor(x),f=fract(x); f=f*f*(3.-2.*f);
  return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
    mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
}
float fbm(vec3 p){ float s=0.,a=.52; for(int i=0;i<5;i++){s+=a*noise(p);p=p*2.03+vec3(1.7,2.3,1.1);a*=.5;}return s; }
void main(){
  vec2 p=(uv*2.-1.)*1.04;
  float r=length(p); if(r>1.){gl_FragColor=vec4(0);return;}
  float z=sqrt(max(0.,1.-dot(p,p)));
  vec3 q=vec3(p*2.2,z*.8);
  q.x+=time*.09; q.y-=time*.055;
  float warp=fbm(q+vec3(0,0,time*.05));
  float n=fbm(q*1.05+warp*(.55+energy*.15));
  float cloud=smoothstep(.20,.66,n*.8-p.y*.6);
  float wisps=fbm(q*3.1+vec3(time*.045,0,2));
  vec3 sky=mix(vec3(.70,.78,1.),vec3(.40,.40,1.),smoothstep(-.7,.8,p.y));
  vec3 white=mix(vec3(.75,.82,1.),vec3(.98,.99,1.),smoothstep(.22,.65,wisps+z*.2));
  vec3 color=mix(sky,white,cloud);
  color+=vec3(.065,.07,.1)*pow(max(0.,p.x*.6-p.y*.4),3.);
  color*=.92+.08*z;
  gl_FragColor=vec4(color,1.-smoothstep(.993,1.,r));
}`

/** Procedural clouds, drawn locally; no video download or generated image needed. */
export function CloudOrb({ level, state }: { level: number; state: AgentState }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const energy = useRef(level)
  useEffect(() => { energy.current = level }, [level])
  useEffect(() => {
    const node = canvas.current
    const gl = node?.getContext('webgl', { alpha: true, premultipliedAlpha: false, antialias: true })
    if (!gl || !node) return
    const compile = (type: number, source: string) => {
      const shader = gl.createShader(type)!
      gl.shaderSource(shader, source); gl.compileShader(shader)
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) { gl.deleteShader(shader); return null }
      return shader
    }
    const vertex = compile(gl.VERTEX_SHADER, VERTEX), fragment = compile(gl.FRAGMENT_SHADER, FRAGMENT)
    if (!vertex || !fragment) { if (vertex) gl.deleteShader(vertex); if (fragment) gl.deleteShader(fragment); return }
    const program = gl.createProgram()!
    gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { gl.deleteProgram(program); gl.deleteShader(vertex); gl.deleteShader(fragment); return }
    gl.useProgram(program)
    const buffer = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW)
    const position = gl.getAttribLocation(program, 'position')
    gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0)
    const clock = gl.getUniformLocation(program, 'time'), volume = gl.getUniformLocation(program, 'energy')
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    let frame = 0, previous = 0, movement = 0
    const draw = (now: number) => {
      const elapsed = Math.min((now - previous) / 1000, .05)
      previous = now
      if (!reduced.matches && !document.hidden) movement += elapsed * (.55 + energy.current * .8)
      const size = Math.round(node.clientWidth * Math.min(devicePixelRatio || 1, 2))
      if (size && node.width !== size) { node.width = size; node.height = size }
      gl.viewport(0, 0, node.width, node.height)
      gl.uniform1f(clock, movement); gl.uniform1f(volume, reduced.matches ? 0 : energy.current)
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
      node.dataset.ready = 'true'
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(frame)
      gl.deleteBuffer(buffer); gl.deleteProgram(program); gl.deleteShader(vertex); gl.deleteShader(fragment)
    }
  }, [])
  return <div className="cloud-orb" data-state={state} aria-hidden="true" style={{ transform: `scale(${1 + level * .065})` }}>
    <canvas ref={canvas} width={480} height={480} />
  </div>
}
