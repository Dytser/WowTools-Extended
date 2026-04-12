#******************************
#---===Import declarations===
#******************************
import bpy
import re

#******************************
#---===GUI===
#******************************
class OBJECT_PT_WoW_Pose(bpy.types.Panel):
	bl_label = 'WoW Pose Tools'
	bl_idname = 'OBJECT_PT_wow_pose_armature_modifer_operators'
	bl_region_type = 'UI'
	bl_space_type = 'VIEW_3D'
	bl_category = 'WoW'
#    bl_space_type: 'PROPERTIES'
#    bl_region_type = 'WINDOW'

#    bl_context = "object"
#    bl_options: {'DRAW_BOX'}
	def draw(self, context):
		layout = self.layout.split()

		layout_col1 = layout.column()

		layout_col1.operator('scene.wow_create_modifiers', text='Create armature modifiers')
		layout_col1.operator('scene.wow_apply_modifiers', text='Apply armature modifiers')
		layout_col1.operator('scene.wow_apply_pose', text='Apply armature pose')
		layout_col1.operator('scene.remove_unused_bones', text='Remove unused bones from armature')
		layout_col1.operator('scene.set_bone_tails', text='Reorient Bones')
		layout_col1.operator('scene.toggle_unused_bones', text='Hide/Show Unused Bones')


class OBJECT_OT_Create_Modifiers(bpy.types.Operator):
	bl_idname = 'scene.wow_create_modifiers'
	bl_label = 'Create armature modifiers'
	bl_description = 'Create armature modifiers on all meshes, which are children to selected armature.'

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'ARMATURE'

	def execute(self, context):
		armature = context.active_object

		for ob in bpy.context.scene.objects:
			if ob.type != 'MESH' or ob.parent != armature:
				continue

			found=False
			#chech mesh already has armature modifier
			for mod in ob.modifiers:
				if mod.type == 'ARMATURE' and mod.object == armature:
					found=True
					break
			if found == False:
				mod = ob.modifiers.new('Armature', 'ARMATURE')
				mod.object = armature
		return {'FINISHED'}

class OBJECT_OT_Apply_Modifiers(bpy.types.Operator):
	bl_idname = 'scene.wow_apply_modifiers'
	bl_label = 'Apply armature modifiers'
	bl_description = 'Apply armature modifiers to all meshes, which are children to selected armature.'

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'ARMATURE'

	def execute(self, context):
		if bpy.context.active_object is None or bpy.context.active_object.type != 'ARMATURE':
			self.report({'ERROR'}, "Armature must be selected")
			return {'FINISHED'}

		armature = bpy.context.active_object
		#bpy.ops.object.select_all(action = 'DESELECT')
		for ob in bpy.context.scene.objects:
			if ob.type == 'MESH':
				bpy.context.view_layer.objects.active = ob
				for mod in ob.modifiers:
					if mod.type == 'ARMATURE' and mod.object == armature:
						bpy.ops.object.modifier_apply(modifier=mod.name)
		return {'FINISHED'}

class OBJECT_OT_Apply_Pose(bpy.types.Operator):
	bl_idname = 'scene.wow_apply_pose'
	bl_label = 'Apply armature pose'
	bl_description = 'Apply currently selected armature pose as rest pose. Original Blender function is bugged - it does not respect attachments.'

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'ARMATURE'

	def execute(self, context):
		if bpy.context.active_object.mode != 'POSE':
			bpy.ops.object.mode_set(mode = 'POSE') 

		BoneByAttach = dict()
		ParentByAttach = dict()
		# detach attachments
		for ob in bpy.context.scene.objects:
			if ob.type == 'EMPTY' and ob.name.startswith('Attach'):
				BoneByAttach[ob.name] = ob.parent_bone
				ParentByAttach[ob.name] = ob.parent
				ob.parent_bone = ''
				ob.parent = None
		#apply pose
		bpy.ops.pose.armature_apply()
		# attach back
		for ob in bpy.context.scene.objects:
			if ob.type == 'EMPTY' and ob.name.startswith('Attach'):
				ob.parent = ParentByAttach[ob.name]
				ob.parent_type = 'BONE'
				ob.parent_bone = BoneByAttach[ob.name]

		return {'FINISHED'}

bpy.types.WindowManager.iWowTools_WeightThreshold = bpy.props.FloatProperty(
	name="iWowTools_WeightThreshold",
	description="maximum vertex weight to be cleared",
	default=0.001,
	subtype='FACTOR',
	unit='NONE',
	min=0.0,
	max=1.0,
	soft_max=1.0)

class DATA_PT_wowtools_vertex_props(bpy.types.Panel):
	bl_label = "Weights cleanup"
	bl_idname = "DATA_PT_wowtools_vertex_props"
	bl_space_type = "PROPERTIES"
	bl_region_type = "WINDOW"
	bl_context = "data"

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'MESH'

	def draw(self, context):

		layout = self.layout

		oWM = context.window_manager

		box = layout.box()
		split = box.split(factor=0.4,align=True)
		col = split.column()
		col.label(text="Weight Threshold:")
		col = split.column()
		col.prop(oWM, "iWowTools_WeightThreshold", text="", slider=True)
		row = box.row()
		row.operator("wowtools.cleanup_weights", text="Cleanup weights")

class DATA_OT_wowtools_cleanup_weights(bpy.types.Operator):

	bl_idname = "wowtools.cleanup_weights"
	bl_label = "Wow Tools Bone Weight Cleanup"
	bl_description = "Removes weights from vertices that are below threshold and remove unused vertex groups"

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'MESH'

	def execute(self, context):
		oTargetObject = context.active_object

		usedVertexGroups = dict()
		for i, Vertex in enumerate(oTargetObject.data.vertices):
			weightsToRemove = []
			for g in Vertex.groups:
				usedVertexGroups[g.group] = 1
				if g.weight < context.window_manager.iWowTools_WeightThreshold:
					weightsToRemove.append(g.group)
					
			for group in weightsToRemove:
				oTargetObject.vertex_groups[group].remove([i])
				
		groupsToRemove = []
		for i, VertexGroup in enumerate(oTargetObject.vertex_groups):
			if VertexGroup.index not in usedVertexGroups:
				groupsToRemove.append(VertexGroup)

		for group in groupsToRemove:
			oTargetObject.vertex_groups.remove(group)


		self.report({'INFO'}, "Vertex weights cleaned up")
		return {'FINISHED'}

class DATA_OT_wowtools_remove_unused_bones(bpy.types.Operator):
	bl_idname = "scene.remove_unused_bones"
	bl_label = "Wow Tools Remove Unused Bones"
	bl_description = "Removes bones from armature that are not used in meshes"

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'ARMATURE'

	def execute(self, context):
		armature = context.active_object

		mode = None
		if armature.mode != 'EDIT':
			mode = armature.mode
			bpy.ops.object.mode_set(mode = 'EDIT')

		usedNames = set()

		for ob in bpy.context.scene.objects:
			usedVertexGroups = set()
			if ob.type == 'MESH' and ob.parent == armature:
				for i, Vertex in enumerate(ob.data.vertices):
					for g in Vertex.groups:
						if g.weight > 0:
							usedVertexGroups.add(g.group)

			for i, VertexGroup in enumerate(ob.vertex_groups):
				if VertexGroup.index in usedVertexGroups:
					usedNames.add(VertexGroup.name)

		removedCnt = 0

		for bone in armature.data.edit_bones:
			if bone.name not in usedNames:
				removedCnt += 1
				armature.data.edit_bones.remove(bone)

		if mode is not None:
			bpy.ops.object.mode_set(mode = mode)

		self.report({'INFO'}, "Removed %d bones from armature" % removedCnt)
		return {'FINISHED'}
	
class DATA_OT_wowtools_toggle_unused_bones(bpy.types.Operator):
	bl_idname = "scene.toggle_unused_bones"
	bl_label = "Wow Tools Toggle Unused Bones"
	bl_description = "Toggles bones from armature that are not used in meshes"

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'ARMATURE'

	def execute(self, context):
		armature = context.active_object
		
		#Edit Bones
		_mode = None
		if armature.mode != 'EDIT':
			_mode = armature.mode
			bpy.ops.object.mode_set(mode = 'EDIT')

		usedNames = set()

		

		for ob in bpy.context.scene.objects:
			usedVertexGroups = set()
			if ob.type == 'MESH' and ob.parent == armature:
				for i, Vertex in enumerate(ob.data.vertices):
					for g in Vertex.groups:
						if g.weight > 0:
							usedVertexGroups.add(g.group)

			for i, VertexGroup in enumerate(ob.vertex_groups):
				if VertexGroup.index in usedVertexGroups:
					usedNames.add(VertexGroup.name)

		removedCnt = 0

		for bone in armature.data.edit_bones:
			if bone.name not in usedNames:
				removedCnt += 1
				bone.hide = not bone.hide

		bpy.ops.object.mode_set(mode = 'POSE')

		for pose_bone in armature.pose.bones:
			if pose_bone.name not in usedNames:
				pose_bone.bone.hide = not pose_bone.bone.hide

		if _mode is None:
			bpy.ops.object.mode_set(mode = 'EDIT')
		if _mode is not None:
			bpy.ops.object.mode_set(mode = _mode)

		self.report({'INFO'}, "Toggles %d bones from armature" % removedCnt)
		return {'FINISHED'}
	
class DATA_OT_wowtools_set_bone_tails(bpy.types.Operator):
	bl_idname = "scene.set_bone_tails"
	bl_label = "WoW Tools Calculate Tail Direction"
	bl_description = "Sets the direction of the tail of each bone to make it look like an actual rig"

	@classmethod
	def poll(cls, context):
		return context.active_object is not None and context.active_object.type == 'ARMATURE'

	def execute(self, context):
		armature = context.active_object

		mode = None
		if armature.mode != 'EDIT':
			mode = armature.mode
			bpy.ops.object.mode_set(mode = 'EDIT')

		for bone in armature.data.edit_bones:
			if bone.parent is not None:
				bone.tail.x = bone.parent.head.x+0.001
				bone.tail.y = bone.parent.head.y+0.001
				bone.tail.z = bone.parent.head.z+0.001
				
				#print(bone.name + 'From: ' + str(bone.tail) + ' to ' + str(bone.parent.head))
				
		#for bone in armature.data.edit_bones:
		#	if bool(bone.parent):
		#		print(bone.name+' has parent')
				
		#	if len(bone.children) is 1:
		#		bone.tail = bone.children[0].head

		#	if len(bone.children) > 1:
		#		for c in bone.children:
		#			if(len(c.children) > 0):
		#				bone.tail = c.head

		if mode is not None:
			bpy.ops.object.mode_set(mode = mode)

		self.report({'INFO'}, "Changed Bone tails ")
		return {'FINISHED'}